from unittest.mock import MagicMock

from rock.sdk.job.config import BashJobConfig
from rock.sdk.job.trial.bash import BashTrial


def test_bash_oss_wrapper_uploads_artifacts_without_metadata_document():
    config = BashJobConfig(script="echo ok", job_name="job")
    config.environment.oss_mirror = MagicMock(enabled=True)
    trial = BashTrial(config)

    wrapper = trial._render_wrapper(config.script)

    assert "ossutil cp" in wrapper
    assert "rock_meta.json" not in wrapper
    assert "__ROCK_META_EOF__" not in wrapper
