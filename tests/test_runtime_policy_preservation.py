"""Image bootstrap/update must retain the no-execution provider policy."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from agentic_host.bootstrap import SetupError, write_environment  # noqa: E402
from agentic_host.updater import write_environment as update_environment  # noqa: E402


@pytest.mark.parametrize('policy', ['grok', 'grok,codex', 'grok, claude', ''])
def test_policy_survives_bootstrap_update_and_image_rollback(tmp_path, policy):
    path = tmp_path / 'runtime.env'
    path.write_text('AGENTIC_DISABLED_PROVIDERS=' + policy + '\n')
    assert update_environment is write_environment
    write_environment(path, 'sha256:' + '1' * 64)
    update_environment(path, 'sha256:' + '2' * 64, 'AGENTIC_CONTROLLER_IMAGE')
    update_environment(path, 'sha256:' + '3' * 64, 'AGENTIC_CONTROLLER_IMAGE')
    update_environment(path, 'sha256:' + '2' * 64, 'AGENTIC_CONTROLLER_IMAGE')
    values = dict(line.split('=', 1) for line in path.read_text().splitlines())
    assert values == {'AGENTIC_DISABLED_PROVIDERS': policy, 'AGENTIC_HERMES_IMAGE': 'sha256:' + '1' * 64,
                      'AGENTIC_CONTROLLER_IMAGE': 'sha256:' + '2' * 64}
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('policy', [
    'unknown', '${OTHER_POLICY}', 'grok,,codex', 'grok,', 'grok;anything', '"grok"',
    'grok\nAGENTIC_DISABLED_PROVIDERS=codex', 'grok\nOTHER_SETTING=value', 'grok' * 40,
])
def test_invalid_policy_blocks_image_change_without_losing_original_file(tmp_path, policy):
    path = tmp_path / 'runtime.env'
    original = 'AGENTIC_DISABLED_PROVIDERS=' + policy + '\n'
    path.write_text(original)
    with pytest.raises(SetupError, match='candidate_environment_invalid'):
        write_environment(path, 'sha256:' + '1' * 64)
    assert path.read_text() == original and not path.with_suffix('.new').exists()


def test_image_activation_cannot_change_administrator_policy(tmp_path):
    path = tmp_path / 'runtime.env'
    path.write_text('AGENTIC_DISABLED_PROVIDERS=grok\n')
    with pytest.raises(SetupError, match='candidate_environment_invalid'):
        write_environment(path, 'sha256:' + '1' * 64, 'AGENTIC_DISABLED_PROVIDERS')
    assert path.read_text() == 'AGENTIC_DISABLED_PROVIDERS=grok\n'
