from rock.actions.sandbox.response import (
    BashObservation,
    CloseBashSessionResponse,
    CommandResponse,
    CreateBashSessionResponse,
    ReadFileResponse,
    UploadResponse,
    WriteFileResponse,
)


class TestResponseContainsCodeAndFailureReason:
    """各 result Response 序列化后应包含 code 和 failure_reason 字段"""

    RESPONSE_CLASSES = [
        CommandResponse,
        BashObservation,
        CreateBashSessionResponse,
        CloseBashSessionResponse,
        ReadFileResponse,
        WriteFileResponse,
        UploadResponse,
    ]

    def test_all_responses_have_code_field(self):
        for cls in self.RESPONSE_CLASSES:
            data = cls().model_dump()
            assert "code" in data, f"{cls.__name__} missing 'code'"

    def test_all_responses_have_failure_reason_field(self):
        for cls in self.RESPONSE_CLASSES:
            data = cls().model_dump()
            assert "failure_reason" in data, f"{cls.__name__} missing 'failure_reason'"
