from session_bridge import __version__
from session_bridge.cli import build_parser


def test_parser_exposes_version() -> None:
    assert __version__ == "0.1.0.dev0"
    assert build_parser().prog == "session-bridge"

