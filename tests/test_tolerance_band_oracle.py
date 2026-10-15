import json
import pytest
from contracts.tolerance_band_oracle import ToleranceBandOracle, gl, _parse_extracted_integer
from contracts.example_consumer import EscrowPredictionSettler


class MockTestEnvironment:
    """Simulates the GenLayer VM environment with leader and validator execution hooks."""
    def __init__(self):
        self.sender = "0xCreator11111111111111111111111111111111"
        self.timestamp = 1000
        self.llm_queue = []
        self.web_content = "<html><body><div id='metric'>42</div></body></html>"

        gl.message.sender_address = self.sender
        gl.message.sender = self.sender
        gl.message.timestamp = self.timestamp

        gl.nondet.web.render = self._mock_web_render
        gl.nondet.web.get = self._mock_web_get
        gl.nondet.exec_prompt = self._mock_exec_prompt
        gl.nondet.llm.prompt = self._mock_exec_prompt
        gl.vm.run_nondet_unsafe = self._mock_run_nondet_unsafe

    def prank(self, sender: str):
        self.sender = sender
        gl.message.sender_address = sender
        gl.message.sender = sender

    def warp(self, timestamp: int):
        self.timestamp = timestamp
        gl.message.timestamp = timestamp

    def enqueue_llm_responses(self, *responses):
        """Queues sequential LLM outputs (e.g. 1st for leader, 2nd for validator)."""
        for r in responses:
            if isinstance(r, (int, float)):
                self.llm_queue.append(json.dumps({"value": int(r)}))
            else:
                self.llm_queue.append(str(r))

    def _mock_web_render(self, url: str, mode: str = "html") -> str:
        return self.web_content

    def _mock_web_get(self, url: str):
        class MockResp:
            body = self.web_content
        return MockResp()

    def _mock_exec_prompt(self, prompt_str: str, **kwargs) -> str:
        if self.llm_queue:
            return self.llm_queue.pop(0)
        return json.dumps({"value": 100})

    def _mock_run_nondet_unsafe(self, leader_fn, validator_fn):
        leader_val = leader_fn()
        wrapped_return = gl.vm.Return(calldata=leader_val)
        agrees = validator_fn(wrapped_return)
        if not agrees:
            raise gl.vm.UserError("Consensus disagreement: validator rejected leader proposal")
        return leader_val


@pytest.fixture
def env():
    return MockTestEnvironment()


@pytest.fixture
def oracle(env):
    return ToleranceBandOracle()


# ---------------------------------------------------------------------------
# Test 1: Invalid register_feed inputs
# ---------------------------------------------------------------------------

def test_invalid_register_feed_inputs(env, oracle):
    # Empty feed_id
    with pytest.raises(gl.vm.UserError, match="feed_id cannot be empty"):
        oracle.register_feed("", "https://api.example.com", "get price", 200, 60)

    # Empty URL
    with pytest.raises(gl.vm.UserError, match="url must be a non-empty HTTP/HTTPS URL"):
        oracle.register_feed("feed-1", "ftp://invalid-protocol", "get price", 200, 60)

    # Empty instruction
    with pytest.raises(gl.vm.UserError, match="instruction cannot be empty"):
        oracle.register_feed("feed-1", "https://api.example.com", "   ", 200, 60)

    # Tolerance too low (< 1 bps)
    with pytest.raises(gl.vm.UserError, match="tolerance_bps must be between 1 and 5000"):
        oracle.register_feed("feed-1", "https://api.example.com", "get price", 0, 60)

    # Tolerance too high (> 5000 bps = 50%)
    with pytest.raises(gl.vm.UserError, match="tolerance_bps must be between 1 and 5000"):
        oracle.register_feed("feed-1", "https://api.example.com", "get price", 5001, 60)

    # Challenge window non-positive
    with pytest.raises(gl.vm.UserError, match="challenge_secs must be strictly positive"):
        oracle.register_feed("feed-1", "https://api.example.com", "get price", 200, 0)

    # Duplicate feed registration
    oracle.register_feed("feed-1", "https://api.example.com/eth", "extract price", 200, 60)
    with pytest.raises(gl.vm.UserError, match="Feed 'feed-1' is already registered"):
        oracle.register_feed("feed-1", "https://api.example.com/eth", "extract price", 200, 60)


# ---------------------------------------------------------------------------
# Test 2: Two validator values inside the tolerance band -> consensus reached
# ---------------------------------------------------------------------------

def test_consensus_reached_when_within_tolerance_band(env, oracle):
    # 200 bps = 2.0% tolerance band
    # Base = 10,000 -> allowed divergence = 10,000 * 200 / 10,000 = 200
    oracle.register_feed("eth-price", "https://api.coinbase.com/v2/prices/ETH-USD/spot", "extract ETH price in USD", 200, 300)

    # Leader observes 10,000; Validator independently observes 10,150 (diff: 150 <= 200)
    env.enqueue_llm_responses(10000, 10150)

    val = oracle.report("eth-price")
    assert val == 10000

    report = json.loads(oracle.get_report("eth-price"))
    assert report["value"] == 10000
    assert report["status"] == "PENDING"
    assert report["challenger"] is None


# ---------------------------------------------------------------------------
# Test 3: Validator value outside the band -> rejected
# ---------------------------------------------------------------------------

def test_consensus_rejected_when_outside_tolerance_band(env, oracle):
    # 200 bps = 2.0% tolerance band
    # Base = 10,000 -> allowed divergence = 200
    oracle.register_feed("btc-price", "https://api.example.com/btc", "extract BTC price", 200, 300)

    # Leader observes 10,000; Validator independently observes 10,250 (diff: 250 > 200)
    env.enqueue_llm_responses(10000, 10250)

    with pytest.raises(gl.vm.UserError, match="validator rejected leader proposal"):
        oracle.report("btc-price")


# ---------------------------------------------------------------------------
# Test 4: Leader returns invalid/malformed non-integer -> rejected
# ---------------------------------------------------------------------------

def test_consensus_rejected_when_leader_returns_invalid_integer(env, oracle):
    oracle.register_feed("bad-feed", "https://api.example.com/bad", "extract score", 100, 120)

    # Leader outputs no number, only gibberish
    env.enqueue_llm_responses("There is no numeric score available on this page.")

    with pytest.raises(gl.vm.UserError, match="Failed to extract a valid integer"):
        oracle.report("bad-feed")


# ---------------------------------------------------------------------------
# Test 5: Defensive markdown fence stripping & JSON parsing
# ---------------------------------------------------------------------------

def test_defensive_markdown_and_format_parsing():
    # Markdown fence with json
    res1 = _parse_extracted_integer("```json\n{\"value\": 4500}\n```")
    assert res1 == 4500

    # Markdown fence without language
    res2 = _parse_extracted_integer("```\n{\"value\": \"$3,200\"}\n```")
    assert res2 == 3200

    # Raw string with currency and text
    res3 = _parse_extracted_integer("The temperature is 78 degrees.")
    assert res3 == 78

    # Negative integer
    res4 = _parse_extracted_integer('{"value": -15}')
    assert res4 == -15


# ---------------------------------------------------------------------------
# Test 6: Challenge that changes the result (divergence > original tolerance)
# ---------------------------------------------------------------------------

def test_challenge_updates_value_when_divergence_exceeds_band(env, oracle):
    # Tolerance 200 bps (2%), window 300 seconds
    oracle.register_feed("stock-tsla", "https://finance.example.com/tsla", "extract TSLA price", 200, 300)

    # Initial report at t=1000: value 1000
    env.warp(1000)
    env.enqueue_llm_responses(1000, 1010)
    oracle.report("stock-tsla")

    # Challenger submits challenge at t=1100 (inside window: 1100 <= 1000 + 300)
    challenger_addr = "0xChallenger22222222222222222222222222222222"
    env.prank(challenger_addr)
    env.warp(1100)

    # Challenge round re-runs extraction with stricter tolerance (half = 100 bps = 1%)
    # Leader and validator both agree the true current value is 1500
    env.enqueue_llm_responses(1500, 1505)

    oracle.challenge("stock-tsla")

    report = json.loads(oracle.get_report("stock-tsla"))
    # Divergence was |1500 - 1000| = 500 > allowed 20 -> status updated to CHALLENGED
    assert report["value"] == 1500
    assert report["status"] == "CHALLENGED"
    assert report["challenger"] == challenger_addr


# ---------------------------------------------------------------------------
# Test 7: Challenge within tolerance band does not overwrite value
# ---------------------------------------------------------------------------

def test_challenge_maintains_original_value_if_within_band(env, oracle):
    oracle.register_feed("temp-nyc", "https://weather.example.com/nyc", "extract temperature", 500, 300)

    # Initial report: 70 degrees (allowed divergence: 70 * 500 / 10000 = 3 degrees)
    env.warp(1000)
    env.enqueue_llm_responses(70, 71)
    oracle.report("temp-nyc")

    # Challenger disputes at t=1100, but new agreed reading is 72 degrees (diff: 2 <= 3)
    env.prank("0xDisputer33333333333333333333333333333333")
    env.warp(1100)
    env.enqueue_llm_responses(72, 72)

    oracle.challenge("temp-nyc")

    report = json.loads(oracle.get_report("temp-nyc"))
    # Original value and PENDING status are maintained
    assert report["value"] == 70
    assert report["status"] == "PENDING"
    assert report["challenger"] is None


# ---------------------------------------------------------------------------
# Test 8: Finalize reverting before challenge window closes
# ---------------------------------------------------------------------------

def test_finalize_reverts_before_window_closes(env, oracle):
    oracle.register_feed("score-game", "https://sports.example.com/game", "extract score", 100, 300)

    env.warp(1000)
    env.enqueue_llm_responses(85, 85)
    oracle.report("score-game")

    # Attempt to finalize at t=1200 (1200 <= 1000 + 300 = 1300) -> must revert
    env.warp(1200)
    with pytest.raises(gl.vm.UserError, match="Challenge window is still open"):
        oracle.finalize("score-game")

    # Finalize at t=1301 (window closed) -> succeeds
    env.warp(1301)
    oracle.finalize("score-game")

    report = json.loads(oracle.get_report("score-game"))
    assert report["status"] == "FINALIZED"


# ---------------------------------------------------------------------------
# Test 9: get_value failing before finalization
# ---------------------------------------------------------------------------

def test_get_value_fails_before_finalization(env, oracle):
    oracle.register_feed("speed-test", "https://sensor.example.com/speed", "extract speed", 100, 200)

    # Non-existent report
    with pytest.raises(gl.vm.UserError, match="No report exists for feed 'speed-test'"):
        oracle.get_value("speed-test")

    # Reported but still PENDING
    env.warp(1000)
    env.enqueue_llm_responses(120, 120)
    oracle.report("speed-test")

    with pytest.raises(gl.vm.UserError, match="is not finalized"):
        oracle.get_value("speed-test")

    # Finalize and retrieve value
    env.warp(1250)
    oracle.finalize("speed-test")
    assert oracle.get_value("speed-test") == 120


# ---------------------------------------------------------------------------
# Test 10: Example consumer contract settling conditionally from oracle
# ---------------------------------------------------------------------------

def test_example_consumer_settlement(env, oracle):
    oracle.register_feed("gold-ounce", "https://commodities.example.com/gold", "extract gold price", 100, 300)

    # Prediction: Gold price >= 2500 settles True, otherwise False
    consumer = EscrowPredictionSettler(
        oracle_address="0xOracleContract000000000000000000000000",
        feed_id="gold-ounce",
        target_threshold=2500
    )

    env.warp(1000)
    env.enqueue_llm_responses(2650, 2650)
    oracle.report("gold-ounce")

    # Attempting to settle consumer before oracle finalizes -> reverts
    with pytest.raises(gl.vm.UserError, match="is not finalized"):
        consumer.settle(oracle_instance=oracle)

    # Finalize oracle report
    env.warp(1350)
    oracle.finalize("gold-ounce")

    # Settle consumer -> succeeds and outcome is True (2650 >= 2500)
    outcome = consumer.settle(oracle_instance=oracle)
    assert outcome is True
    assert consumer.settled is True
    assert consumer.outcome is True

    # Cannot settle again
    with pytest.raises(gl.vm.UserError, match="already settled"):
        consumer.settle(oracle_instance=oracle)
