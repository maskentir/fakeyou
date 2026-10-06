from importlib.resources import files


def load_capture_script() -> str:
    return (files("fakeyou") / "assets" / "stream_capture.js").read_text(encoding="utf-8")
