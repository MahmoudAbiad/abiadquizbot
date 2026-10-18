"""Wallet failures distinguish an RPC error from a confirmed balance rejection."""


class WalletDebitError(RuntimeError):
    """Debit failed or its result could not be confirmed; never implies low funds."""


WALLET_DEBIT_ERROR_TEXT = (
    "⚠️ تعذر تأكيد عملية الرصيد بسبب خطأ تقني، ولم تبدأ المعالجة. "
    "يرجى المحاولة بعد قليل. إذا تغيّر رصيدك دون تنفيذ الطلب، تواصل مع الدعم."
)
