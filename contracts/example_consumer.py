# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
try:
    from genlayer import *
except (ImportError, ModuleNotFoundError):
    from contracts.tolerance_band_oracle import gl, Address, bigint


class EscrowPredictionSettler(gl.Contract):
    """
    Consumer contract demonstrating cross-contract composability with ToleranceBandOracle.
    Fetches finalized integer values from the oracle to settle conditional escrows.
    """
    oracle_address: Address
    feed_id: str
    target_threshold: bigint
    settled: bool
    outcome: bool

    def __init__(self, oracle_address: Address, feed_id: str, target_threshold: int):
        self.oracle_address = Address(str(oracle_address))
        self.feed_id = str(feed_id)
        self.target_threshold = bigint(target_threshold)
        self.settled = False
        self.outcome = False

    @gl.public.write
    def settle(self, oracle_instance=None) -> bool:
        if self.settled:
            raise gl.vm.UserError("Escrow is already settled")
        oracle = oracle_instance if oracle_instance else gl.get_contract_at(self.oracle_address)
        final_value = oracle.get_value(self.feed_id)
        self.outcome = bool(int(final_value) >= self.target_threshold)
        self.settled = True
        return self.outcome
