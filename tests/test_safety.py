"""core/safety 单测：中英危险动作识别 + confirm 注入 reader。全 mock。"""
from core import safety


def test_detects_dangerous_english():
    assert safety.is_dangerous("Delete account") is True
    assert safety.is_dangerous("Place order") is True
    assert safety.is_dangerous("Send message") is True
    assert safety.is_dangerous("Submit") is True
    assert safety.is_dangerous("Pay now") is True


def test_detects_dangerous_chinese():
    assert safety.is_dangerous("提交订单") is True
    assert safety.is_dangerous("支付") is True
    assert safety.is_dangerous("删除商品") is True
    assert safety.is_dangerous("发送消息") is True
    assert safety.is_dangerous("立即下单") is True


def test_safe_labels_are_not_flagged():
    for label in ("Search", "登录", "Next page", "搜索框", "Cancel", "返回首页",
                  "input[type=text]", ""):
        assert safety.is_dangerous(label) is False, label


def test_match_dangerous_returns_keyword():
    assert safety.match_dangerous("请点击 提交 按钮") == "提交"
    assert safety.match_dangerous("Delete this file") == "delete"
    assert safety.match_dangerous("nothing here") is None
    assert safety.match_dangerous(None) is None


def test_describe_shape():
    assert safety.describe("提交订单") == {"dangerous": True, "keyword": "提交"}
    assert safety.describe("Search") == {"dangerous": False, "keyword": None}


def test_confirm_accepts_affirmative_from_injected_reader():
    seen = []

    def reader(prompt):
        seen.append(prompt)
        return "y\n"

    assert safety.confirm("提交订单", reader=reader) is True
    assert seen and "提交订单" in seen[0]


def test_confirm_rejects_negative_and_empty():
    assert safety.confirm("提交订单", reader=lambda p: "n") is False
    assert safety.confirm("提交订单", reader=lambda p: "") is False
    assert safety.confirm("提交订单", reader=lambda p: "   ") is False
    assert safety.confirm("提交订单", reader=lambda p: None) is False
    assert safety.confirm("提交订单", reader=lambda p: "no") is False


def test_confirm_treats_reader_failure_as_refusal():
    def broken(prompt):
        raise RuntimeError("stdin 不可用")

    assert safety.confirm("删除账号", reader=broken) is False


def test_confirm_supports_chinese_affirmative():
    assert safety.confirm("支付", reader=lambda p: "是") is True
    assert safety.confirm("支付", reader=lambda p: "确认") is True
    assert safety.confirm("支付", reader=lambda p: "算了") is False


def test_is_affirmative_edges():
    assert safety.is_affirmative(" YES ") is True
    assert safety.is_affirmative("1") is True
    assert safety.is_affirmative("") is False
    assert safety.is_affirmative(None) is False
    assert safety.is_affirmative("maybe") is False
