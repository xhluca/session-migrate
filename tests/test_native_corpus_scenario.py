import hashlib
import json
import struct
import wave
from pathlib import Path

CORPUS_ROOT = Path(__file__).parent / "native_corpus" / "v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_scenario_assets_are_content_addressed_and_cover_media_families() -> None:
    scenario = json.loads((CORPUS_ROOT / "scenario.json").read_text())
    assets = {item["media_type"]: item for item in scenario["assets"]}

    assert set(assets) == {
        "application/pdf",
        "audio/wav",
        "image/jpeg",
        "image/png",
        "video/mp4",
    }
    for item in assets.values():
        path = CORPUS_ROOT / item["path"]
        assert path.is_file()
        assert _sha256(path) == item["sha256"]


def test_image_audio_document_and_video_assets_have_expected_native_shapes() -> None:
    png = (CORPUS_ROOT / "assets/corpus-card.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (640, 400)

    jpeg = (CORPUS_ROOT / "assets/corpus-card.jpg").read_bytes()
    assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"

    with wave.open(str(CORPUS_ROOT / "assets/corpus-tone.wav"), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 16_000
        assert audio.getnframes() == 4_000

    document = (CORPUS_ROOT / "assets/corpus-document.pdf").read_bytes()
    assert document.startswith(b"%PDF-1.4")
    assert document.rstrip().endswith(b"%%EOF")

    video = (CORPUS_ROOT / "assets/corpus-transition.mp4").read_bytes()
    assert video[4:12] == b"ftypisom"


def test_scenario_has_deterministic_prompts_and_no_secret_placeholders() -> None:
    scenario = json.loads((CORPUS_ROOT / "scenario.json").read_text())

    assert scenario["scenario_id"] == "portable-rich-v1"
    assert len(scenario["turns"]) == 4
    assert {item["modality"] for item in scenario["media_attempts"]} == {
        "audio",
        "document",
        "user_image",
        "video",
    }
    serialized = json.dumps(scenario).lower()
    assert "api_key" not in serialized
    assert "password" not in serialized
    assert "sk-or-" not in serialized
