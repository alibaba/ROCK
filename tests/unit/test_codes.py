import logging

import rock
from rock._codes import codes

logger = logging.getLogger(__name__)


def test_codes_values():
    """测试基本状态码值"""
    assert rock.codes.OK == 2000
    assert rock.codes.BAD_REQUEST == 4000
    assert rock.codes.INTERNAL_SERVER_ERROR == 5000
    assert rock.codes.COMMAND_ERROR == 6000
    logger.info(f"rock.codes.OK.phrase: {rock.codes.OK.phrase}")
    logger.info(f"rock.codes.BAD_REQUEST: {rock.codes.BAD_REQUEST}")


def test_codes_phrases():
    """测试状态码的phrase属性"""
    assert rock.codes.OK.phrase == "OK"
    assert rock.codes.BAD_REQUEST.phrase == "Bad Request"
    assert rock.codes.INTERNAL_SERVER_ERROR.phrase == "Internal Server Error"
    assert rock.codes.COMMAND_ERROR.phrase == "Command Error"


def test_codes_string_representation():
    """测试状态码的字符串表示"""
    assert str(rock.codes.OK) == "2000"
    assert str(rock.codes.BAD_REQUEST) == "4000"
    assert str(rock.codes.INTERNAL_SERVER_ERROR) == "5000"
    assert str(rock.codes.COMMAND_ERROR) == "6000"


def test_get_reason_phrase():
    """测试get_reason_phrase方法"""
    assert codes.get_reason_phrase(2000) == "OK"
    assert codes.get_reason_phrase(4000) == "Bad Request"
    assert codes.get_reason_phrase(5000) == "Internal Server Error"
    assert codes.get_reason_phrase(6000) == "Command Error"

    # 测试不存在的状态码
    assert codes.get_reason_phrase(9999) == ""
    assert codes.get_reason_phrase(1000) == ""


def test_is_success():
    """测试is_success方法"""
    assert codes.is_success(2000) is True
    assert codes.is_success(2001) is True
    assert codes.is_success(2999) is True

    assert codes.is_success(1999) is False
    assert codes.is_success(3000) is False
    assert codes.is_success(4000) is False
    assert codes.is_success(5000) is False


def test_is_client_error():
    """测试is_client_error方法"""
    assert codes.is_client_error(4000) is True
    assert codes.is_client_error(4001) is True
    assert codes.is_client_error(4999) is True

    assert codes.is_client_error(3999) is False
    assert codes.is_client_error(5000) is False
    assert codes.is_client_error(2000) is False


def test_is_server_error():
    """测试is_server_error方法"""
    assert codes.is_server_error(5000) is True
    assert codes.is_server_error(5001) is True
    assert codes.is_server_error(5999) is True

    assert codes.is_server_error(4999) is False
    assert codes.is_server_error(6000) is False
    assert codes.is_server_error(2000) is False


def test_is_command_error():
    """测试is_command_error方法"""
    assert codes.is_command_error(6000) is True
    assert codes.is_command_error(6001) is True
    assert codes.is_command_error(6999) is True

    assert codes.is_command_error(5999) is False
    assert codes.is_command_error(7000) is False
    assert codes.is_command_error(2000) is False


def test_is_error():
    """测试is_error方法"""
    # 客户端错误
    assert codes.is_error(4000) is True
    assert codes.is_error(4999) is True

    # 服务器错误
    assert codes.is_error(5000) is True
    assert codes.is_error(5999) is True

    # 命令错误
    assert codes.is_error(6000) is True
    assert codes.is_error(6999) is True

    # 非错误状态
    assert codes.is_error(2000) is False
    assert codes.is_error(3000) is False
    assert codes.is_error(3999) is False
    assert codes.is_error(7000) is False


def test_lowercase_compatibility():
    """测试小写属性兼容性"""
    assert hasattr(codes, "ok")
    assert hasattr(codes, "bad_request")
    assert hasattr(codes, "internal_server_error")
    assert hasattr(codes, "command_error")

    assert codes.ok == 2000
    assert codes.bad_request == 4000
    assert codes.internal_server_error == 5000
    assert codes.command_error == 6000


def test_enum_behavior():
    """测试枚举行为"""
    # 测试枚举成员比较
    assert rock.codes.OK == codes.OK
    assert rock.codes.BAD_REQUEST != rock.codes.OK

    # 测试枚举成员类型
    assert isinstance(rock.codes.OK, codes)
    assert isinstance(rock.codes.OK, int)

    # 测试枚举迭代
    all_codes = list(codes)
    assert len(all_codes) == 4
    assert codes.OK in all_codes
    assert codes.BAD_REQUEST in all_codes
    assert codes.INTERNAL_SERVER_ERROR in all_codes
    assert codes.COMMAND_ERROR in all_codes


def test_boundary_values():
    """测试边界值"""
    # 测试范围边界
    assert codes.is_success(2000) is True
    assert codes.is_success(2999) is True
    assert codes.is_success(1999) is False
    assert codes.is_success(3000) is False

    assert codes.is_client_error(4000) is True
    assert codes.is_client_error(4999) is True
    assert codes.is_client_error(3999) is False
    assert codes.is_client_error(5000) is False

    assert codes.is_server_error(5000) is True
    assert codes.is_server_error(5999) is True
    assert codes.is_server_error(4999) is False
    assert codes.is_server_error(6000) is False

    assert codes.is_command_error(6000) is True
    assert codes.is_command_error(6999) is True
    assert codes.is_command_error(5999) is False
    assert codes.is_command_error(7000) is False
