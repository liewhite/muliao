"""以太坊签名（eth_sign 工具，#423）：用 vault 里的私钥在本进程内签交易、EIP-191 消息、EIP-712 typed data。

私钥只在这里的调用期间以明文存在于内存：不经子进程环境变量、命令行与磁盘，也不出现在返回值、错误与日志里。只签名
不广播。不提供对任意 32 字节 hash 的裸签：裸签的 hash 可能就是某笔交易的签名哈希，等于替人签了一笔看不见内容的交易；
EIP-191 消息带前缀、EIP-712 带域，签出的都不是合法交易签名。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

from muliao.errors import InvalidInput


class SignKind(StrEnum):
    TRANSACTION = "transaction"
    MESSAGE = "message"
    TYPED_DATA = "typed_data"


@dataclass(frozen=True)
class Signature:
    """一次签名的结果（eth_sign 工具原样返回）。"""

    # 签名者地址（EIP-55 校验和格式）
    signer: str
    # kind=transaction：签好的 raw tx（0x hex，可直接 eth_sendRawTransaction）与 tx hash
    raw_transaction: str | None = None
    tx_hash: str | None = None
    # kind=message / typed_data：65 字节签名 r||s||v（0x hex）与被签的 32 字节摘要（审计日志也记它）
    signature: str | None = None
    message_hash: str | None = None


def _checked[T](call: Callable[[], T], error: str, *, with_cause: bool = True) -> T:
    """调 eth-account：它抛的任何异常都是输入不合约定（输入全部来自模型，库的异常体系不统一：rlp、eth_abi、eth_utils
    的异常不继承 ValueError），转成 InvalidInput 交还模型。在 except 块外再抛，原异常不挂在 __context__ 上（私钥的
    异常文本可能带私钥）；with_cause=False 时连原异常的文本也不带。"""
    try:
        return call()
    except Exception as e:
        cause = f": {e}" if with_cause else ""
    raise InvalidInput(error + cause)


def sign(private_key: str, key: str, kind: SignKind, payload: str | dict[str, Any]) -> Signature:
    """用 private_key（vault 里名为 key 的 secret 的明文）签 payload。payload 不合 kind 的约定即 InvalidInput。"""
    account = _checked(
        lambda: Account.from_key(private_key),
        f"secret {key!r} is not a valid Ethereum private key (32 bytes hex)",
        with_cause=False,
    )
    match kind:
        case SignKind.TRANSACTION:
            return _sign_transaction(account, payload)
        case SignKind.MESSAGE:
            return _sign_message(account, payload)
        case SignKind.TYPED_DATA:
            return _sign_typed_data(account, payload)


def _sign_transaction(account: LocalAccount, payload: str | dict[str, Any]) -> Signature:
    if not isinstance(payload, dict):
        raise InvalidInput("kind=transaction needs payload to be an object of transaction fields")
    # 没有 chainId（或为 null、0）的 legacy 交易签出来是 EIP-155 之前的格式或无意义的链，任何链都能重放
    chain_id = payload.get("chainId")
    if isinstance(chain_id, str) and chain_id.startswith("0x"):
        chain_id = _checked(lambda: int(chain_id, 16), "invalid chainId")
    if not chain_id:
        raise InvalidInput("the transaction must include a non-zero chainId")
    signed = _checked(lambda: account.sign_transaction(payload), "invalid transaction")
    return Signature(
        signer=account.address,
        raw_transaction=signed.raw_transaction.to_0x_hex(),
        tx_hash=signed.hash.to_0x_hex(),
    )


def _sign_message(account: LocalAccount, payload: str | dict[str, Any]) -> Signature:
    if not isinstance(payload, str):
        raise InvalidInput(
            "kind=message needs payload to be a string: text, or 0x-prefixed hex for raw bytes"
        )
    message = _checked(
        lambda: encode_defunct(hexstr=payload) if payload.startswith("0x") else encode_defunct(text=payload),
        "invalid message",
    )
    signed = account.sign_message(message)
    return Signature(
        signer=account.address,
        signature=signed.signature.to_0x_hex(),
        message_hash=signed.message_hash.to_0x_hex(),
    )


def _sign_typed_data(account: LocalAccount, payload: str | dict[str, Any]) -> Signature:
    if not isinstance(payload, dict):
        raise InvalidInput(
            "kind=typed_data needs payload to be the full EIP-712 object with types, primaryType, domain and message"
        )
    signed = _checked(lambda: account.sign_typed_data(full_message=payload), "invalid typed data")
    return Signature(
        signer=account.address,
        signature=signed.signature.to_0x_hex(),
        message_hash=signed.message_hash.to_0x_hex(),
    )
