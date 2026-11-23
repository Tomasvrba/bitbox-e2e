"""Headless control of the graphical BitBox02 simulator (test/simulator-graphical).

The simulator is a winit/OpenGL window that reacts only to pointer *position*: the cursor entering
the top/bottom slider strip is a touch, leaving it is a release. We drive it with XTest motion on an
Xvfb display and read pixels back with GetImage. Geometry mirrors the constants at the top of
test/simulator-graphical/src/main.rs; update them together.
"""

import hashlib
import json
import os
import re
import select
import socket
import subprocess
import time
from pathlib import Path

from PIL import Image
from Xlib import X, display as xdisplay
from Xlib.ext import xtest

import bundle

MARGIN = 20
PADDING_TOP_BOTTOM = 22
PADDING_LEFT = 60
PADDING_RIGHT = 35
SCREEN_W = 128
SCREEN_H = 64
WIN_W = 2 * MARGIN + PADDING_LEFT + SCREEN_W + PADDING_RIGHT
WIN_H = 2 * MARGIN + 2 * PADDING_TOP_BOTTOM + SCREEN_H
# Device display rectangle inside the window.
LCD_BOX = (MARGIN + PADDING_LEFT, MARGIN + PADDING_TOP_BOTTOM,
           MARGIN + PADDING_LEFT + SCREEN_W, MARGIN + PADDING_TOP_BOTTOM + SCREEN_H)

TOP_Y = MARGIN * 3 // 4            # inside [MARGIN/2, MARGIN)
BOTTOM_Y = WIN_H - MARGIN * 3 // 4  # inside [H-MARGIN, H-MARGIN/2)
IDLE = (WIN_W // 2, WIN_H // 2)    # over the screen, touches nothing
# Right of the device, vertically in the middle third of the LCD: both sliders at 255.
PINCH = (WIN_W - PADDING_RIGHT // 3, MARGIN + PADDING_TOP_BOTTOM + SCREEN_H // 2)


def slider_x(pos):
    """Window x for a slider position 0..255 (inverse of the simulator's mapping)."""
    span = SCREEN_W + PADDING_LEFT // 2 + PADDING_RIGHT
    return MARGIN + PADDING_LEFT // 2 + (pos * span + 254) // 255


class Xvfb:
    """Xvfb on a display it picks itself (-displayfd): the first one without a lock file in /tmp,
    which network namespaces share, so concurrent runs never collide. It writes the number once it
    accepts clients; a socket file left by a dead server can't pass for that. self.disp is ":<n>"."""

    def __init__(self, log_path, timeout_s=10):
        self.log = open(log_path, "w")
        r, w = os.pipe()
        self.proc = subprocess.Popen(
            ["Xvfb", "-displayfd", str(w), "-screen", "0", "640x480x24", "-nolisten", "tcp"],
            stdout=self.log, stderr=self.log, pass_fds=(w,))
        os.close(w)
        got, deadline = b"", time.time() + timeout_s
        try:
            while not got.endswith(b"\n") and select.select([r], [], [], max(0, deadline - time.time()))[0]:
                chunk = os.read(r, 16)
                if not chunk:
                    break  # Xvfb exited
                got += chunk
        finally:
            os.close(r)
        if not got.strip().isdigit():
            self.stop()
            raise RuntimeError(f"Xvfb did not start, see {log_path}")
        self.disp = f":{got.decode().strip()}"

    def stop(self):
        self.proc.terminate()
        self.proc.wait(5)
        self.log.close()


class GraphicalSim:
    def __init__(self, binary, disp, log_path, fake_memory_dir, port=None):
        """port=None: no --port, the simulator binds 15423 (all of them can, older ones only that)."""
        env = dict(os.environ, DISPLAY=disp, FAKE_MEMORY_FILEPATH=fake_memory_dir + "/fm")
        self.port = port or bundle.SIM_PORT
        self.log = open(log_path, "w")
        self.proc = subprocess.Popen([binary, "--preseed", *(["--port", str(port)] if port else [])], env=env,
                                     stdout=self.log, stderr=self.log)
        self.d = xdisplay.Display(disp)
        self.root = self.d.screen().root
        self.move(*IDLE)
        self._wait_window()

    def _wait_window(self):
        for _ in range(200):
            if self.proc.poll() is not None:
                raise RuntimeError(f"simulator-graphical exited with {self.proc.returncode}")
            if self.root.query_tree().children and self._has_content():
                return
            time.sleep(0.05)
        raise RuntimeError("simulator window never rendered")

    def _has_content(self):
        return self.window_image().getextrema() != ((0, 0), (0, 0), (0, 0))

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    # Without a window manager the window sits at the root origin.
    def window_image(self):
        raw = self.root.get_image(0, 0, WIN_W, WIN_H, X.ZPixmap, 0xFFFFFFFF)
        return Image.frombytes("RGB", (WIN_W, WIN_H), raw.data, "raw", "BGRX")

    def lcd_image(self, scale=4):
        img = self.window_image().crop(LCD_BOX)
        return img.resize((SCREEN_W * scale, SCREEN_H * scale), Image.NEAREST)

    def lcd_hash(self):
        """Screen identity. The last LCD row is the animated scroll-position hint on scrollable
        screens, so it is left out; otherwise those screens never look stable."""
        x0, y0, x1, y1 = LCD_BOX
        return hashlib.sha256(self.window_image().crop((x0, y0, x1, y1 - 1)).tobytes()).hexdigest()

    def move(self, x, y):
        xtest.fake_input(self.d, X.MotionNotify, x=x, y=y)
        self.d.sync()

    def _touch(self, y, pos, hold_s, release_y):
        self.move(slider_x(pos), y)
        time.sleep(hold_s)
        self.move(slider_x(pos), release_y)
        time.sleep(0.05)
        self.move(*IDLE)

    def tap(self, side="bottom", pos=255, hold_s=0.08):
        if side == "bottom":
            self._touch(BOTTOM_Y, pos, hold_s, WIN_H - 2)
        else:
            self._touch(TOP_Y, pos, hold_s, 2)

    def pinch(self, max_s=20.0):
        """Touch both sliders at their right end (the simulator's pinch hitbox) until the screen
        settles: released once it has been still for 0.3s after changing (a hold-to-confirm
        animates until it fires, at a frame-rate dependent speed, so no fixed hold time fits a
        loaded machine), or for 1s if it never changed. Note the release is also a tap at 255 on
        both sliders, delivered to whatever screen is up by then."""
        start = last = self.lcd_hash()
        self.move(*PINCH)
        t0 = since = time.time()
        while time.time() - t0 < max_s:
            time.sleep(0.03)
            h = self.lcd_hash()
            if h != last:
                last, since = h, time.time()
            elif time.time() - since >= (1.0 if last == start else 0.3):
                break
        self.move(*IDLE)

    def accepting(self):
        """Whether the simulator keeps a client: until its startup task (orientation, logo) is
        done it accepts TCP connections and drops them right away."""
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1) as s:
                s.settimeout(0.3)
                return s.recv(1) != b""
        except socket.timeout:
            return True
        except OSError:
            return False

    def choose_orientation(self, timeout_s=30.0):
        """Tap "Tap this side" at the bottom until the simulator accepts clients. Taps are ignored
        while the orientation screen's intro animation runs, so one tap at a fixed time is not
        enough, and the auto-advance policy must not run here: its top tap means upside down."""
        # A tap that lands shows the logo for 1.3s, then the lockscreen, and the startup task ends.
        # Later taps hit the logo or lockscreen, which ignore them.
        deadline = time.time() + timeout_s
        while time.time() < deadline and self.proc.poll() is None:
            self.tap("bottom", 128)
            if self.accepting():
                # Let the simulator see the probe's disconnect before the driver connects.
                time.sleep(0.1)
                return
            time.sleep(0.3)
        shot = Path(self.log.name).with_name("startup-failure.png")
        self.window_image().save(shot)
        raise RuntimeError(f"simulator never finished startup (orientation screen), see {shot}")

    def wait_stable(self, settle_s=0.4, timeout_s=10.0):
        """Wait until the LCD stops changing (animations, scrolling text) and return its hash."""
        deadline = time.time() + timeout_s
        last, since = self.lcd_hash(), time.time()
        while time.time() < deadline:
            time.sleep(0.03)
            h = self.lcd_hash()
            if h != last:
                last, since = h, time.time()
            elif time.time() - since >= settle_s:
                return h
        return last


class Recorder:
    """Records a run on the graphical simulator and advances the device. Finishes the orientation
    screen, then saves every new stable LCD screen to `out` as a PNG labelled with `step`.

    Policy for a screen that stays unchanged: tap the top-right control (accept/next), then the
    hold-to-confirm gesture. Never top-left, which rejects. `setup_steps` are the steps before the
    requests under test (unlock, a random pairing code, idle screens); `prefix` goes in front of
    the file names in the frame list."""

    def __init__(self, sim, out, setup_steps, prefix=""):
        self.sim, self.out, self.setup_steps, self.prefix = sim, Path(out), setup_steps, prefix
        self.frames, self.step, self.last, self.tried = [], "boot", None, 0
        self.actions = [lambda: sim.tap("top", 255), sim.pinch]
        sim.choose_orientation()
        self.capture(sim.wait_stable())

    def capture(self, h):
        name = f"{len(self.frames):02d}-{re.sub(r'[^a-z0-9]+', '-', self.step.lower())}.png"
        self.sim.lcd_image().save(self.out / name)
        self.frames.append({"file": self.prefix + name, "step": self.step, "hash": h})
        self.last, self.tried = h, 0

    def settle(self, settle_s=0.5):
        """Waits for a stable screen and captures it if it is new. Returns whether it was. A new
        screen gets a full settle period before act(): some take input only after they have been
        up a while, e.g. the password entry ignores an immediate tap."""
        h = self.sim.wait_stable(settle_s=settle_s)
        if h == self.last:
            return False
        self.capture(h)
        return True

    def act(self):
        """The policy's next action on the current, unchanged screen. Not on the idle screen between
        requests: the host's next request can land while the pinch is held, and its release then
        taps (accepts) that request's first screen before it was captured (seen in the WalletConnect
        flow: the unusual-keypath warning missing)."""
        idle = self.step not in self.setup_steps and any(
            f["hash"] == self.last for f in self.frames if f["step"] in self.setup_steps)
        if self.tried < len(self.actions) and not idle:
            self.actions[self.tried]()
            self.tried += 1
        else:
            time.sleep(0.1)  # waiting on the host, or nothing we know how to press

    def finish(self, frames_json, final_is_idle=True):
        """Drops idle screens inside request steps, writes and returns the frame list. Between two
        requests the device shows its idle screen for as long as the host takes, so whether a step
        captures it is a race. Idle means seen during setup and, with final_is_idle, the screen the
        run ended on; the final frame itself stays."""
        frames = self.frames
        idle = {f["hash"] for f in frames if f["step"] in self.setup_steps}
        if final_is_idle and frames:
            idle.add(frames[-1]["hash"])
        keep = []
        for i, f in enumerate(frames):
            if f["step"] in self.setup_steps or f["hash"] not in idle or (final_is_idle and i == len(frames) - 1):
                keep.append(f)
            else:
                (self.out / Path(f["file"]).name).unlink()
        Path(frames_json).write_text(json.dumps(keep, indent=2))
        return keep
