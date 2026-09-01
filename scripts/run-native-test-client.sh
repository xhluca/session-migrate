#!/usr/bin/env bash
# Run every credential-free exact-client gate assigned to one pinned harness.

set -euo pipefail

client="${1:?usage: run-native-test-client.sh CLIENT ENV_FILE}"
env_file="${2:?usage: run-native-test-client.sh CLIENT ENV_FILE}"
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
if [[ -n "${SESSION_MIGRATE_NATIVE_PATH:-}" ]]; then
  export PATH="$SESSION_MIGRATE_NATIVE_PATH:$PATH"
fi

case "$client" in
  antigravity)
    tests=(
      tests/test_antigravity_native.py::test_antigravity_1116_loads_adapter_database_and_continues_offline
    )
    ;;
  claude)
    tests=(
      tests/test_claude_codex_corpus_native.py::test_exact_claude_cold_reloads_sanitized_corpus_source
      'tests/test_claude_codex_corpus_native.py::test_exact_client_resumes_fresh_writer_output[claude-SESSION_MIGRATE_CLAUDE_BIN-2.1.209-b882f4b8b27772f897540df50f24000206f43a9426e8f7d19bd065959b69e9dd]'
    )
    ;;
  codex)
    tests=(
      tests/test_claude_codex_corpus_native.py::test_exact_codex_cold_reloads_sanitized_corpus_source
      'tests/test_claude_codex_corpus_native.py::test_exact_client_resumes_fresh_writer_output[codex-SESSION_MIGRATE_CODEX_BIN-0.144.4-2b3edc9cdfd1717fba3dbc92817205a8a2c7511d459e456d4817eeff6f78ed7a]'
    )
    ;;
  copilot)
    tests=(tests/test_copilot_source_native.py)
    ;;
  cursor)
    tests=(
      tests/test_cursor_native.py::test_pinned_cursor_loads_renders_and_serves_imported_history
    )
    ;;
  devin)
    tests=(
      tests/test_devin_native.py
      tests/test_devin_corpus_native.py::test_exact_devin_cold_reloads_sanitized_corpus_source
    )
    ;;
  grok)
    tests=(
      tests/test_grok_kilo_openhands_native.py::test_grok_105_creates_native_multimodal_tool_source_from_empty_state
      tests/test_grok_kilo_openhands_native.py::test_grok_105_loads_prefix_and_appends_through_loopback
      tests/test_grok_kilo_openhands_native.py::test_grok_105_cold_reloads_sanitized_native_corpus_source
    )
    ;;
  hermes)
    tests=(
      tests/test_hermes_native.py
      tests/test_hermes_corpus_source.py::test_hermes_fixture_cold_reloads_and_continues_in_exact_client
    )
    ;;
  kilo)
    tests=(
      tests/test_grok_kilo_openhands_native.py::test_kilo_750_official_import_replay_and_export
      'tests/test_opencode_kilo_corpus_native.py::test_exact_client_captures_from_empty_through_public_surfaces[kilo]'
      'tests/test_opencode_kilo_corpus_native.py::test_exact_client_cold_import_export_and_continuation_preserve_prefix[kilo]'
    )
    ;;
  kimi)
    tests=(
      tests/test_muse_qwen_kimi_native.py::test_kimi_0380_offline_resume_replays_imported_history
      tests/test_qwen_kimi_corpus_native.py::test_exact_kimi_cold_reloads_sanitized_corpus_source
    )
    ;;
  mastracode)
    tests=(
      tests/test_mastracode_native.py
      tests/test_mastracode_corpus_source.py::test_mastracode_fixture_cold_reloads_and_continues_in_exact_client
    )
    ;;
  muse)
    tests=(
      tests/test_muse_qwen_kimi_native.py::test_muse_021_offline_resume_replays_imported_history
      tests/test_muse_corpus_source.py::test_exact_muse_cold_reloads_and_continues_sanitized_fixture
    )
    ;;
  omp)
    tests=(
      tests/test_omp_native.py
      'tests/test_pi_omp_corpus_native.py::test_exact_pi_omp_native_from_empty_media_tools_and_cold_reload[omp]'
      'tests/test_pi_omp_corpus_native.py::test_exact_pi_omp_public_fixture_cold_reload[omp-fixture1]'
    )
    ;;
  opencode)
    tests=(
      tests/test_additional_formats_native.py::test_opencode_11720_official_import_and_loopback_resume
      tests/test_additional_formats_native.py::test_opencode_cli_import_uses_official_importer_and_rejects_native_collision
      tests/test_additional_formats_native.py::test_opencode_native_replay_preserves_source_order_with_decreasing_timestamps
      'tests/test_opencode_kilo_corpus_native.py::test_exact_client_captures_from_empty_through_public_surfaces[opencode]'
      'tests/test_opencode_kilo_corpus_native.py::test_exact_client_cold_import_export_and_continuation_preserve_prefix[opencode]'
    )
    ;;
  openhands)
    tests=(
      tests/test_grok_kilo_openhands_native.py::test_openhands_1160_creates_native_tool_source_from_empty_state
      tests/test_grok_kilo_openhands_native.py::test_openhands_1160_reloads_sanitized_native_source
      'tests/test_grok_kilo_openhands_native.py::test_openhands_1160_rejects_binary_media_on_its_text_file_surface[corpus-card.png]'
      'tests/test_grok_kilo_openhands_native.py::test_openhands_1160_rejects_binary_media_on_its_text_file_surface[corpus-document.pdf]'
      'tests/test_grok_kilo_openhands_native.py::test_openhands_1160_rejects_binary_media_on_its_text_file_surface[corpus-tone.wav]'
      'tests/test_grok_kilo_openhands_native.py::test_openhands_1160_rejects_binary_media_on_its_text_file_surface[corpus-transition.mp4]'
      tests/test_grok_kilo_openhands_native.py::test_openhands_1160_loads_prefix_and_appends_through_loopback
    )
    ;;
  pi)
    tests=(
      tests/test_additional_formats_native.py::test_pi_0806_loads_compaction_images_and_tools_via_offline_rpc
      'tests/test_pi_omp_corpus_native.py::test_exact_pi_omp_native_from_empty_media_tools_and_cold_reload[pi]'
      'tests/test_pi_omp_corpus_native.py::test_exact_pi_omp_public_fixture_cold_reload[pi-fixture0]'
    )
    ;;
  qwen)
    tests=(
      tests/test_muse_qwen_kimi_native.py::test_qwen_0221_offline_resume_replays_imported_history
      tests/test_qwen_kimi_corpus_native.py::test_exact_qwen_cold_reloads_sanitized_corpus_source
    )
    ;;
  vibe)
    tests=(tests/test_vibe_native.py)
    ;;
  *)
    printf 'Unknown native client: %s\n' "$client" >&2
    exit 2
    ;;
esac

report_file="$(mktemp "${TMPDIR:-/tmp}/session-migrate-native-junit.XXXXXX.xml")"
trap 'rm -f -- "$report_file"' EXIT
uv run pytest -q --junitxml="$report_file" "${tests[@]}"
uv run python scripts/assert_junit_no_skips.py "$report_file"
