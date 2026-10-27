from src.silo import KINDS, flow_default_kind, resolve_kind

def test_horton_transcript_is_a_known_kind():
    assert "horton_transcript" in KINDS

def test_horton_flow_default():
    assert flow_default_kind("horton") == "horton_transcript"
    assert resolve_kind(flow_default_kind("horton")) == "horton_transcript"
