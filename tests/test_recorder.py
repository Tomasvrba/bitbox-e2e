"""gsim.Recorder, the device policy every graphical run shares, on a fake simulator."""

from PIL import Image

from bitbox_e2e import gsim


class FakeSim:
    """Returns the given screen hashes, one per wait_stable(); records what the policy pressed."""

    def __init__(self, screens):
        self.screens, self.pressed = iter(screens), []

    def choose_orientation(self):
        pass

    def wait_stable(self, settle_s=0.4):
        return next(self.screens)

    def lcd_image(self):
        return Image.new("RGB", (4, 4))

    def tap(self, side, pos):
        self.pressed.append(f"tap {side}")

    def pinch(self):
        self.pressed.append("pinch")


def test_accepts_a_request_screen_but_leaves_the_idle_screen_alone(tmp_path):
    sim = FakeSim(["lockscreen", "lockscreen", "confirm"])
    rec = gsim.Recorder(sim, tmp_path, {"boot"})
    rec.step = "sign"
    assert not rec.settle()
    rec.act()  # the setup-time screen between requests: the host's next request may be on its way
    assert sim.pressed == []
    assert rec.settle()
    rec.act()
    rec.act()
    rec.act()  # policy exhausted
    assert sim.pressed == ["tap top", "pinch"]


def recorded(tmp_path, hashes):
    rec = gsim.Recorder(FakeSim([hashes[0]]), tmp_path, {"boot"})
    rec.step = "sign"
    for h in hashes[1:]:
        rec.capture(h)
    return rec


def test_finish_drops_idle_screens_inside_requests_but_keeps_the_final_frame(tmp_path):
    rec = recorded(tmp_path, ["lock", "confirm", "lock", "home", "fee", "home"])
    kept = rec.finish(tmp_path / "frames.json")
    assert [f["hash"] for f in kept] == ["lock", "confirm", "fee", "home"]
    assert sorted(p.name for p in tmp_path.glob("*.png")) == sorted(f["file"] for f in kept)


def test_finish_without_final_idle_keeps_screens_equal_to_the_last(tmp_path):
    rec = recorded(tmp_path, ["lock", "confirm", "lock", "home", "fee", "home"])
    kept = rec.finish(tmp_path / "frames.json", final_is_idle=False)
    assert [f["hash"] for f in kept] == ["lock", "confirm", "home", "fee", "home"]
