"""以太坊签名（#423）：签出的交易、消息、typed data 能还原出签名者；不合约定的输入在第一现场报错且不泄露私钥。"""

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from muliao.agents.eth_signing import SignKind, sign
from muliao.errors import InvalidInput

KEY = "0x" + "11" * 32
ADDRESS = Account.from_key(KEY).address
TO = "0x" + "22" * 20

EIP1559 = {
    "chainId": 1,
    "nonce": "0x1",
    "to": TO,
    "value": "0xde0b6b3a7640000",
    "gas": 21000,
    "maxFeePerGas": 2 * 10**9,
    "maxPriorityFeePerGas": 10**9,
    "data": "0x",
}
LEGACY = {"chainId": 56, "nonce": 0, "to": TO, "value": 1, "gas": 21000, "gasPrice": 10**9}
TYPED = {
    "types": {
        "EIP712Domain": [
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "SafeTx": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
    },
    "primaryType": "SafeTx",
    "domain": {"chainId": 1, "verifyingContract": TO},
    "message": {"to": TO, "value": 5},
}


@pytest.mark.parametrize("tx", [EIP1559, LEGACY], ids=["eip1559", "legacy"])
def test_signed_transaction_recovers_to_the_signer(tx):
    result = sign(KEY, "K", SignKind.TRANSACTION, tx)

    assert result.signer == ADDRESS
    assert Account.recover_transaction(result.raw_transaction) == ADDRESS
    assert result.tx_hash is not None and result.tx_hash.startswith("0x") and len(result.tx_hash) == 66
    assert result.signature is None


def test_key_without_0x_prefix_works():
    assert sign(KEY.removeprefix("0x"), "K", SignKind.MESSAGE, "hi").signer == ADDRESS


@pytest.mark.parametrize(
    "tx",
    [
        {k: v for k, v in LEGACY.items() if k != "chainId"},
        {**LEGACY, "chainId": None},
        {**LEGACY, "chainId": 0},
        {**LEGACY, "chainId": "0x00"},
    ],
    ids=["missing", "null", "zero", "hex-zero"],
)
def test_transaction_without_chain_id_is_refused(tx):
    # 没有 chainId 的 legacy 签名可在任何链重放
    with pytest.raises(InvalidInput, match="chainId"):
        sign(KEY, "K", SignKind.TRANSACTION, tx)


@pytest.mark.parametrize(
    ("message", "signable"),
    [("hello", encode_defunct(text="hello")), ("0xdeadbeef", encode_defunct(hexstr="0xdeadbeef"))],
    ids=["text", "hex-bytes"],
)
def test_message_is_personal_signed(message, signable):
    result = sign(KEY, "K", SignKind.MESSAGE, message)

    assert result.signer == ADDRESS
    assert Account.recover_message(signable, signature=result.signature) == ADDRESS
    assert result.raw_transaction is None and result.message_hash is not None


def test_typed_data_is_signed_per_eip712():
    result = sign(KEY, "K", SignKind.TYPED_DATA, TYPED)

    assert (
        Account.recover_message(encode_typed_data(full_message=TYPED), signature=result.signature) == ADDRESS
    )


@pytest.mark.parametrize(
    ("kind", "payload", "error"),
    [
        (SignKind.TRANSACTION, "0x02f8", "object of transaction fields"),
        (SignKind.MESSAGE, {"text": "hi"}, "string"),
        (SignKind.TYPED_DATA, "{}", "full EIP-712 object"),
        (SignKind.MESSAGE, "0xzz", "invalid message"),
        (SignKind.TRANSACTION, {**LEGACY, "gas": "lots"}, "invalid transaction"),
        (SignKind.TYPED_DATA, {"types": {}}, "invalid typed data"),
        # 库抛的不继承 ValueError 的异常（rlp、eth_abi）也是输入错误
        (SignKind.TRANSACTION, {**LEGACY, "nonce": -1}, "invalid transaction"),
        (SignKind.TYPED_DATA, {**TYPED, "message": {"to": "0x12", "value": 5}}, "invalid typed data"),
    ],
)
def test_payload_not_matching_kind_is_invalid_input(kind, payload, error):
    with pytest.raises(InvalidInput, match=error):
        sign(KEY, "K", kind, payload)


def test_bad_private_key_error_does_not_show_the_key():
    secret = "not-a-key-but-secret-text"
    with pytest.raises(InvalidInput) as e:
        sign(secret, "K", SignKind.MESSAGE, "hi")

    assert "'K' is not a valid Ethereum private key" in str(e.value)
    assert secret not in str(e.value) and e.value.__cause__ is None and e.value.__context__ is None
