"""
image_transfer.py

Sender/receiver library for the SUAS map-image transfer system — built
entirely on STANDARD MAVLink messages already present in any stock
pymavlink install. No custom dialect, no mavgen, no generated bindings
to copy anywhere.

Wire format (all standard messages, zero setup required):
  1. STATUSTEXT "IMGMETA:<image_id_hex>:<crc32_hex>:<filename>"
     Carries the metadata DATA_TRANSMISSION_HANDSHAKE has no room for.
  2. DATA_TRANSMISSION_HANDSHAKE(type=1, size, width=0, height=0,
     packets, payload=253, jpg_quality=0)
     Describes the transfer itself.
  3. ENCAPSULATED_DATA(seqnr, data[253]) x N
     The actual bytes, one packet each.
  4. STATUSTEXT "IMGDONE:<image_id_hex>"
     Sent after each send pass (initial, and after every resend batch).
     This is a NUDGE, not a completion claim -- the sender keeps sending
     it periodically until it hears back one of:
  5. STATUSTEXT "IMGRESEND:<image_id_hex>:<seq1>,<seq2>,..."
     Receiver -> sender. Specific missing packet numbers. Sender resends
     just those, then sends IMGDONE again.
  6. STATUSTEXT "IMGACK:<image_id_hex>"
     Receiver -> sender. Sent once the file is fully reassembled AND its
     CRC32 has been verified. THIS is what the sender actually waits for
     to consider the transfer complete -- not silence, not a round count.
  7. STATUSTEXT "IMGFAIL:<image_id_hex>"
     Receiver -> sender. Sent if the CRC32 check fails even after all
     packets were received (corruption, not loss) -- tells the sender to
     stop waiting rather than retry forever against an unrecoverable error.

The sender will keep nudging with IMGDONE and resending whatever's asked
for indefinitely, up to cfg.max_total_wait_s of *wall clock* time (a
safety net against a truly dead link, not a "give up after N tries"
limit) -- it only stops early on an explicit IMGACK or IMGFAIL.

Copy this file to BOTH the Raspberry Pi and the ground station backend.
"""

import os
import time
import zlib
import threading
from dataclasses import dataclass
from pathlib import Path

IMGMETA_PREFIX = "IMGMETA:"
IMGDONE_PREFIX = "IMGDONE:"
IMGRESEND_PREFIX = "IMGRESEND:"
IMGACK_PREFIX = "IMGACK:"
IMGFAIL_PREFIX = "IMGFAIL:"
CHUNK_SIZE = 253   # fixed by the MAVLink ENCAPSULATED_DATA message -- do not change


# ============================================================
# Configuration
# ============================================================

@dataclass
class ImageTransferConfig:
    max_rate_kbps: float = 32.0       # hard ceiling on transfer bandwidth -- see note below
    min_rate_kbps: float = 4.0        # floor when adaptive mode backs off
    adaptive: bool = True             # adjust rate based on observed heartbeat health
    start_ramp_fraction: float = 0.5  # adaptive mode starts at this fraction of max_rate_kbps
    output_dir: str = "received_images"   # receiver-side only
    timeout_s: float = 60.0           # receiver: abandon if NO packet at all arrives for this long
    resend_wait_s: float = 4.0        # sender: how long to wait per round for a resend/ack/fail
    max_total_wait_s: float = 600.0   # sender: overall safety net (wall clock) -- not a round count


DEFAULT_CONFIG = ImageTransferConfig()

# A note on max_rate_kbps: 32 kbps assumes your radio link has that much
# headroom alongside normal flight telemetry. The old conservative 8 kbps
# default meant a 200KB image took 3+ minutes. Since the protocol now
# retries specific missing packets until the ground station explicitly
# confirms receipt (see IMGACK above), pushing the rate higher is much
# safer than it used to be -- a burst of packet loss from going too fast
# now just means a slightly longer retry phase, not a stuck transfer.
# If your link genuinely can't sustain this, either lower this value or
# let adaptive mode do its job (it backs off automatically when heartbeat
# gaps grow, i.e. when the link is visibly straining).


# ============================================================
# CRC32 (integrity check)
# ============================================================

def compute_crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ============================================================
# Adaptive rate limiter (sender-side)
# ============================================================

class AdaptiveRateLimiter:
    """
    Tracks HEARTBEAT arrival gaps on the same connection being used to
    send image packets, and adjusts the send rate accordingly.
    """
    def __init__(self, config: ImageTransferConfig = DEFAULT_CONFIG):
        self.cfg = config
        self.adaptive = config.adaptive
        self.max_rate = config.max_rate_kbps
        self.min_rate = config.min_rate_kbps
        self.current_rate = (config.max_rate_kbps * config.start_ramp_fraction
                             if config.adaptive else config.max_rate_kbps)
        self._last_hb_time = None

    def note_heartbeat(self, timestamp: float = None):
        now = timestamp if timestamp is not None else time.time()
        if self._last_hb_time is not None and self.adaptive:
            gap = now - self._last_hb_time
            if gap > 2.0:
                self.current_rate = max(self.min_rate, self.current_rate * 0.7)
            elif gap < 1.3:
                self.current_rate = min(self.max_rate, self.current_rate * 1.1)
        self._last_hb_time = now

    def delay_for(self, packet_bytes: int) -> float:
        rate_bytes_per_s = max(self.current_rate, 0.5) * 1000 / 8
        return packet_bytes / rate_bytes_per_s


# ============================================================
# Sender (Raspberry Pi side)
# ============================================================

class ImageSender:
    def __init__(self, config: ImageTransferConfig = DEFAULT_CONFIG,
                progress_cb=None, status_cb=None):
        self.cfg = config
        self.progress_cb = progress_cb or (lambda *a: None)
        self.status_cb = status_cb or print
        self._lock = threading.Lock()

    def _send_packet(self, master, seq: int, data: bytes):
        chunk = data[seq * CHUNK_SIZE: seq * CHUNK_SIZE + CHUNK_SIZE]
        if len(chunk) < CHUNK_SIZE:
            chunk = chunk + b"\x00" * (CHUNK_SIZE - len(chunk))
        with self._lock:
            master.mav.encapsulated_data_send(seq, chunk)

    def _send_done(self, master, image_id: int):
        with self._lock:
            master.mav.statustext_send(0, f"{IMGDONE_PREFIX}{image_id:08x}".encode()[:50])

    def send(self, master, image_path: str, image_id: int = None) -> bool:
        """Blocking — run on its own thread if the caller shouldn't be
        blocked. Does NOT consider the transfer done until the ground
        station explicitly confirms (IMGACK) or reports failure (IMGFAIL)
        -- it keeps nudging and resending whatever's requested until then,
        bounded only by cfg.max_total_wait_s of wall-clock time."""
        image_path = Path(image_path)
        if not image_path.exists():
            self.status_cb(f"[IMG] ERROR: file not found: {image_path}")
            return False

        data = image_path.read_bytes()
        total_size = len(data)
        crc = compute_crc32(data)
        total_packets = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        image_id = image_id if image_id is not None else int(time.time()) & 0xFFFFFFFF
        filename = image_path.name[:32]   # keep the combined STATUSTEXT under 50 bytes

        self.status_cb(f"[IMG] Sending {image_path.name}: {total_size} bytes, "
                       f"{total_packets} packets, crc32={crc:08x}")

        meta = f"{IMGMETA_PREFIX}{image_id:08x}:{crc:08x}:{filename}"
        with self._lock:
            master.mav.statustext_send(0, meta.encode("utf-8")[:50])
        time.sleep(0.1)

        with self._lock:
            master.mav.data_transmission_handshake_send(
                1, total_size, 0, 0, total_packets, CHUNK_SIZE, 0)
        time.sleep(0.3)   # give the receiver a moment to set up before the chunk flood starts

        limiter = AdaptiveRateLimiter(self.cfg)
        last_pct = -1
        for i in range(total_packets):
            hb = master.recv_match(type="HEARTBEAT", blocking=False)
            if hb is not None:
                limiter.note_heartbeat()

            self._send_packet(master, i, data)
            time.sleep(limiter.delay_for(CHUNK_SIZE + 20))   # +~20 bytes MAVLink framing overhead

            pct = int((i + 1) / total_packets * 100)
            self.progress_cb(i + 1, total_packets, pct)
            if pct >= last_pct + 5:
                last_pct = pct
                # Plain ASCII only -- a multi-byte char truncated mid-character
                # by the 50-byte STATUSTEXT limit corrupts the whole string.
                self.status_cb(f"Sending packet {i + 1} / {total_packets} - Progress: {pct}%")

        self._send_done(master, image_id)
        self.status_cb(f"[IMG] First pass complete: {image_path.name} — "
                       f"waiting for ground station confirmation...")

        # Wait for IMGACK / IMGFAIL, resending whatever's requested, until
        # confirmed either way or max_total_wait_s wall-clock time elapses.
        overall_deadline = time.time() + self.cfg.max_total_wait_s
        round_num = 0
        while time.time() < overall_deadline:
            round_num += 1
            round_deadline = time.time() + self.cfg.resend_wait_s
            heard_anything = False

            while time.time() < round_deadline:
                msg = master.recv_match(type="STATUSTEXT", blocking=False)
                if msg is not None:
                    text = (msg.text or "").rstrip("\x00")
                    if text.startswith(IMGACK_PREFIX):
                        if self._id_matches(text, IMGACK_PREFIX, image_id):
                            self.status_cb("[IMG] Ground station confirmed receipt ✓ — transfer complete.")
                            return True
                    elif text.startswith(IMGFAIL_PREFIX):
                        if self._id_matches(text, IMGFAIL_PREFIX, image_id):
                            self.status_cb("[IMG] Ground station reported failure (CRC mismatch) — stopping.")
                            return False
                    elif text.startswith(IMGRESEND_PREFIX):
                        missing = self._parse_resend(text, image_id)
                        if missing:
                            heard_anything = True
                            self.status_cb(f"[IMG] Resending {len(missing)} missing packet(s) "
                                          f"(round {round_num})")
                            for seq in missing:
                                hb = master.recv_match(type="HEARTBEAT", blocking=False)
                                if hb is not None:
                                    limiter.note_heartbeat()
                                self._send_packet(master, seq, data)
                                time.sleep(limiter.delay_for(CHUNK_SIZE + 20))
                            self._send_done(master, image_id)
                time.sleep(0.05)

            if not heard_anything:
                # Total silence this round -- our IMGDONE or their IMGACK
                # may have been lost. Nudge again rather than assume either.
                self._send_done(master, image_id)
                self.status_cb(f"[IMG] Still waiting for confirmation (round {round_num})...")

        self.status_cb(f"[IMG] Timed out after {self.cfg.max_total_wait_s:.0f}s "
                       f"waiting for ground station confirmation.")
        return False

    @staticmethod
    def _id_matches(text: str, prefix: str, image_id: int) -> bool:
        try:
            return int(text[len(prefix):], 16) == image_id
        except Exception:
            return False

    @staticmethod
    def _parse_resend(text: str, image_id: int):
        try:
            _, id_hex, seqs_str = text.split(":", 2)
            if int(id_hex, 16) != image_id or not seqs_str:
                return []
            return [int(s) for s in seqs_str.split(",") if s]
        except Exception:
            return []


# ============================================================
# Receiver (ground station side)
# ============================================================

class ImageReceiver:
    """
    Feed every incoming MAVLink message through .handle_message(msg) — it
    returns True if the message was consumed as part of the image protocol.

    on_ack(image_id, ok: bool) MUST be provided for the sender's
    confirmation-based completion to work at all -- wire it to send an
    IMGACK (ok=True) or IMGFAIL (ok=False) STATUSTEXT back to the Pi over
    your normal read/write MAVLink connection (not a read-only listener
    connection, if you have one of those).

    on_resend_request(image_id, missing_seqs) is called whenever the
    sender nudges with IMGDONE and packets are still missing -- wire it
    the same way, to ask for specifically those packet numbers.
    """
    def __init__(self, config: ImageTransferConfig = DEFAULT_CONFIG,
                on_progress=None, on_complete=None, on_log=None,
                on_resend_request=None, on_ack=None):
        self.cfg = config
        self.on_progress = on_progress or (lambda *a: None)
        self.on_complete = on_complete or (lambda *a: None)
        self.on_log = on_log or print
        self.on_resend_request = on_resend_request or (lambda *a: None)
        self.on_ack = on_ack or (lambda *a: None)
        self._pending_meta = None   # (image_id, crc32, filename) waiting for a handshake
        self._transfer = None
        self._last_result = None    # (image_id, ok) -- lets a repeated IMGDONE nudge get re-ack'd
                                     # even after we've already finished and cleared _transfer

    def handle_message(self, msg) -> bool:
        mtype = msg.get_type()
        if mtype == "STATUSTEXT":
            text = (msg.text or "").rstrip("\x00")
            if text.startswith(IMGMETA_PREFIX):
                self._on_meta(text)
                return True
            if text.startswith(IMGDONE_PREFIX):
                self._on_done_signal(text)
                return True
            return False
        elif mtype == "DATA_TRANSMISSION_HANDSHAKE":
            self._on_handshake(msg)
            return True
        elif mtype == "ENCAPSULATED_DATA":
            self._on_data(msg)
            return True
        return False

    def check_timeout(self) -> None:
        """Safety net only -- abandons if literally nothing has arrived
        for cfg.timeout_s. Does NOT cap how many resend rounds happen;
        that's bounded on the sender side by max_total_wait_s instead."""
        t = self._transfer
        if t is not None and time.time() - t["last_progress"] > self.cfg.timeout_s:
            self.on_log(f"[IMG] Transfer stalled ({self.cfg.timeout_s:.0f}s no data at all) — abandoning.")
            self.on_complete(None, False, "stalled")
            self._transfer = None

    def _on_meta(self, text):
        try:
            _, image_id_hex, crc_hex, filename = text.split(":", 3)
            self._pending_meta = (int(image_id_hex, 16), int(crc_hex, 16), filename or "map.jpg")
        except Exception:
            self._pending_meta = None

    def _on_handshake(self, msg):
        if self._pending_meta:
            image_id, crc32, filename = self._pending_meta
        else:
            image_id, crc32, filename = int(time.time()) & 0xFFFFFFFF, None, "received_map.jpg"
        self._pending_meta = None

        self._transfer = {
            "image_id": image_id, "crc32": crc32, "filename": filename,
            "total_size": msg.size, "total_packets": msg.packets,
            "chunks": {}, "received": 0, "last_progress": time.time(),
        }
        note = "" if crc32 is not None else " (no CRC metadata received -- will skip verification)"
        self.on_log(f"[IMG] Incoming: {filename}, {msg.size}B, {msg.packets} packets{note}")
        self.on_progress(0, msg.packets, 0)

    def _on_data(self, msg):
        t = self._transfer
        if t is None:
            return   # handshake missed or stale -- ignore
        seq = msg.seqnr
        if seq not in t["chunks"]:
            t["chunks"][seq] = bytes(msg.data)
            t["received"] += 1
            pct = int(t["received"] / t["total_packets"] * 100)
            self.on_log(f"Received: {t['received']} / {t['total_packets']} - Progress: {pct}%")
            self.on_progress(t["received"], t["total_packets"], pct)
        t["last_progress"] = time.time()

    def _on_done_signal(self, text):
        """Sender is nudging: 'nothing left to send on this pass, are we
        done?'. If we already fully finished THIS exact transfer, just
        re-ack -- the ack itself may be what got lost, not the data."""
        try:
            _, image_id_hex = text.split(":", 1)
            image_id = int(image_id_hex, 16)
        except Exception:
            return

        if self._last_result is not None and self._last_result[0] == image_id:
            self.on_ack(image_id, self._last_result[1])
            return

        t = self._transfer
        if t is None or t["image_id"] != image_id:
            return   # unrelated/stale nudge -- ignore

        missing = [i for i in range(t["total_packets"]) if i not in t["chunks"]]
        if not missing:
            self._finish()
            return

        t["last_progress"] = time.time()
        self.on_log(f"[IMG] {len(missing)} packet(s) missing — requesting resend")
        self.on_resend_request(image_id, missing)

    def _finish(self):
        t = self._transfer
        image_id = t["image_id"]
        try:
            ordered = b"".join(t["chunks"][i] for i in range(t["total_packets"]))
            data = ordered[:t["total_size"]]

            if t["crc32"] is not None:
                crc = compute_crc32(data)
                if crc != t["crc32"]:
                    self.on_log(f"[IMG] CRC32 mismatch: expected {t['crc32']:08x}, got {crc:08x}")
                    self._last_result = (image_id, False)
                    self.on_ack(image_id, False)
                    self.on_complete(None, False, "CRC32 mismatch")
                    return

            os.makedirs(self.cfg.output_dir, exist_ok=True)
            safe_name = f"{image_id}_{t['filename']}"
            path = os.path.join(self.cfg.output_dir, safe_name)
            with open(path, "wb") as f:
                f.write(data)
            verified = "CRC32 verified" if t["crc32"] is not None else "no CRC available"
            self.on_log(f"[IMG] Saved {path} ({len(data)} bytes, {verified})")
            self._last_result = (image_id, True)
            self.on_ack(image_id, True)
            self.on_complete(path, True, None)
        except Exception as e:
            self.on_log(f"[IMG] Failed to save: {e}")
            self._last_result = (image_id, False)
            self.on_ack(image_id, False)
            self.on_complete(None, False, str(e))
        finally:
            self._transfer = None
