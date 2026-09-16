from __future__ import annotations

from src import twikit_runtime


def test_twikit_transaction_patch_is_applied_and_idempotent():
    transaction = __import__(
        "twikit.x_client_transaction.transaction",
        fromlist=["ClientTransaction"],
    )

    twikit_runtime._APPLIED = False
    assert twikit_runtime.apply_twikit_transaction_patch() is True
    patched = transaction.ClientTransaction.get_indices
    assert patched.__module__ == "src.twikit_runtime"

    # Reapplying must not wrap/replace the method a second time.
    assert twikit_runtime.apply_twikit_transaction_patch() is True
    assert transaction.ClientTransaction.get_indices is patched
