# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
import re
from typing import Optional, Any

try:
    from genlayer import *
except (ImportError, ModuleNotFoundError):
    # Standalone mock environment for local unit testing and development
    class u256(int):
        def __new__(cls, val=0):
            return super().__new__(cls, int(val))

    class bigint(int):
        pass

    class Address(str):
        pass

    class TreeMap(dict):
        pass

    class MockVM:
        class UserError(ValueError):
            pass

        class Return:
            def __init__(self, calldata):
                self.calldata = calldata

        class Rollback(Exception):
            pass

        @staticmethod
        def run_nondet_unsafe(leader_fn, validator_fn):
            leader_val = leader_fn()
            wrapped_return = MockVM.Return(calldata=leader_val)
            is_valid = validator_fn(wrapped_return)
            if not is_valid:
                raise MockVM.UserError("Consensus disagreement: validator rejected leader proposal")
            return leader_val

    class MockGL:
        class MockMessage:
            sender_address: str = "0xCreator11111111111111111111111111111111"
            sender: str = "0xCreator11111111111111111111111111111111"
            timestamp: int = 1000

        class MockNondet:
            class MockWeb:
                @staticmethod
                def render(url: str, mode: str = "html") -> str:
                    return f"<html><body><div id='price'>100</div></body></html>"

                @staticmethod
                def get(url: str):
                    class MockResponse:
                        body = "<html><body><div id='price'>100</div></body></html>"
                    return MockResponse()

            class MockLLM:
                @staticmethod
                def prompt(prompt_str: str) -> str:
                    return '{"value": 100}'

            web = MockWeb()
            llm = MockLLM()

            @staticmethod
            def exec_prompt(prompt_str: str, **kwargs) -> str:
                return '{"value": 100}'

        class PublicDecorators:
            @staticmethod
            def write(fn):
                return fn

            @staticmethod
            def view(fn):
                return fn

        vm = MockVM()
        message = MockMessage()
        nondet = MockNondet()
        public = PublicDecorators()

        class Contract:
            pass

    gl = MockGL()


# ---------------------------------------------------------------------------
# Helper Utility Functions
# ---------------------------------------------------------------------------

def _clean_markdown_fence(raw_text: str) -> str:
    """Strips markdown code fences (```json ... ``` or ``` ...) defensively."""
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_extracted_integer(raw_llm_output: str) -> int:
    """
    Defensively extracts an integer from LLM response.
    Supports JSON output format: {"value": 1234}
    Falls back to regex integer extraction if plain text is returned.
    Raises gl.vm.UserError if no valid integer can be parsed.
    """
    cleaned = _clean_markdown_fence(raw_llm_output)

    # First attempt: structured JSON parsing
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "value" in parsed:
            val = parsed["value"]
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                # Clean any currency symbols, commas or whitespace
                sanitized = re.sub(r"[^\d\-+]", "", val)
                if sanitized:
                    return int(sanitized)
        elif isinstance(parsed, (int, float)):
            return int(parsed)
    except Exception:
        pass

    # Second attempt: regex search for JSON-like "value": <digits>
    m = re.search(r'"value"\s*:\s*(-?\d+)', cleaned)
    if m:
        return int(m.group(1))

    # Third attempt: first standalone integer in text
    standalone = re.search(r'(?<!\w)(-?\d+)(?!\w)', cleaned)
    if standalone:
        return int(standalone.group(1))

    raise gl.vm.UserError(
        f"Failed to extract a valid integer from LLM response: '{raw_llm_output[:120]}'"
    )


# ---------------------------------------------------------------------------
# Core Contract: ToleranceBandOracle
# ---------------------------------------------------------------------------

class ToleranceBandOracle(gl.Contract):
    """
    Intelligent Contract Primitive: ToleranceBandOracle

    A decentralized oracle that extracts real-world integer metrics from the web
    (e.g., token prices, temperature, scores, follower counts) using GenLayer's
    Equivalence Principle with a numeric tolerance band instead of strict equality.

    Consensus Rationale:
    1. Real-world live data (such as financial markets, live sports, or web traffic)
       fluctuates sub-second. If validators demanded strict string or value equality,
       natural latency between validator fetches would cause consensus to fail.
    2. By parameterizing tolerance in basis points (1 bps = 0.01%), validators accept
       any observation that falls within:
           |validator_val - leader_val| <= max(|leader_val|, 1) * tolerance_bps / 10,000
    3. An optimistic challenge window allows anyone to dispute an erroneous report
       using a stricter tolerance band (half the feed's tolerance) before finalization.
    """

    # Persistent Storage
    feeds: TreeMap[str, str]
    reports: TreeMap[str, str]

    # Report Status Constants
    STATUS_PENDING = "PENDING"
    STATUS_CHALLENGED = "CHALLENGED"
    STATUS_FINALIZED = "FINALIZED"

    def __init__(self):
        self.feeds = TreeMap()
        self.reports = TreeMap()

    # -----------------------------------------------------------------------
    # Internal Helpers
    # -----------------------------------------------------------------------

    def _get_current_timestamp(self) -> int:
        return int(getattr(gl.message, "timestamp", 0))

    def _get_sender(self) -> str:
        sender = getattr(gl.message, "sender_address", None)
        if not sender:
            sender = getattr(gl.message, "sender", "0x0000000000000000000000000000000000000000")
        return str(sender)

    def _fetch_web_content(self, url: str) -> str:
        """Fetches page content defensively via gl.nondet.web."""
        try:
            if hasattr(gl.nondet.web, "render"):
                content = gl.nondet.web.render(url, mode="html")
                if content:
                    return str(content)[:8000]
        except Exception:
            pass

        try:
            if hasattr(gl.nondet.web, "get"):
                resp = gl.nondet.web.get(url)
                if hasattr(resp, "body"):
                    return str(resp.body)[:8000]
                return str(resp)[:8000]
        except Exception as e:
            raise gl.vm.UserError(f"Web fetch failed for URL '{url}': {str(e)}")

        raise gl.vm.UserError(f"Web fetch produced no usable data for URL '{url}'")

    def _extract_metric(self, url: str, instruction: str) -> int:
        """
        Nondeterministic fetch and extraction routine executed by leader and validators.
        """
        page_content = self._fetch_web_content(url)

        prompt = (
            f"You are a high-precision data extraction oracle.\n"
            f"Task: Extract the numeric value based on the instruction below.\n\n"
            f"INSTRUCTION:\n{instruction}\n\n"
            f"PAGE CONTENT EVIDENCE:\n{page_content}\n\n"
            f"REQUIREMENT:\n"
            f"Extract ONLY the requested single integer number.\n"
            f"Respond with a valid JSON object in the exact format:\n"
            f'{{"value": <integer>}}\n'
            f"Do not include any other markdown, text, or explanations."
        )

        llm_output = ""
        if hasattr(gl.nondet, "exec_prompt"):
            llm_output = gl.nondet.exec_prompt(prompt)
        elif hasattr(gl.nondet, "llm") and hasattr(gl.nondet.llm, "prompt"):
            llm_output = gl.nondet.llm.prompt(prompt)
        else:
            raise gl.vm.UserError("No LLM execution engine available on validator node")

        return _parse_extracted_integer(llm_output)

    # -----------------------------------------------------------------------
    # Public State-Modifying Methods
    # -----------------------------------------------------------------------

    @gl.public.write
    def register_feed(
        self,
        feed_id: str,
        url: str,
        instruction: str,
        tolerance_bps: int,
        challenge_secs: int
    ) -> None:
        """
        Registers a new real-world data feed specification.

        Validation rules:
        - feed_id: Non-empty string, must not already be registered.
        - url: Non-empty string starting with http:// or https://.
        - instruction: Non-empty extraction directive.
        - tolerance_bps: 1 to 5000 (0.01% to 50.00%).
        - challenge_secs: Greater than 0.
        """
        fid = (feed_id or "").strip()
        if not fid:
            raise gl.vm.UserError("feed_id cannot be empty")

        if self.feeds.get(fid, None) is not None:
            raise gl.vm.UserError(f"Feed '{fid}' is already registered")

        u = (url or "").strip()
        if not u or not (u.startswith("http://") or u.startswith("https://")):
            raise gl.vm.UserError("url must be a non-empty HTTP/HTTPS URL")

        instr = (instruction or "").strip()
        if not instr:
            raise gl.vm.UserError("instruction cannot be empty")

        tol = int(tolerance_bps)
        if tol < 1 or tol > 5000:
            raise gl.vm.UserError(
                f"tolerance_bps must be between 1 and 5000 (got {tol})"
            )

        c_secs = int(challenge_secs)
        if c_secs <= 0:
            raise gl.vm.UserError(
                f"challenge_secs must be strictly positive (got {c_secs})"
            )

        owner = self._get_sender()

        feed_data = {
            "feed_id": fid,
            "url": u,
            "instruction": instr,
            "tolerance_bps": tol,
            "challenge_secs": c_secs,
            "owner": owner
        }

        self.feeds[fid] = json.dumps(feed_data)

    @gl.public.write
    def report(self, feed_id: str) -> int:
        """
        Executes an observation round for a registered feed.

        Consensus Mechanism:
        1. Leader fetches the URL and prompts LLM to extract an integer.
        2. Each validator independently re-fetches and re-extracts an integer.
        3. Validator accepts the leader's proposal iff:
             |validator_val - leader_val| <= max(|leader_val|, 1) * tolerance_bps / 10,000
        4. Rejects if leader's proposal is an error, non-integer, or outside tolerance.
        5. On consensus, stores report in PENDING state to begin the challenge window.
        """
        fid = (feed_id or "").strip()
        raw_feed = self.feeds.get(fid, None)
        if raw_feed is None:
            raise gl.vm.UserError(f"Feed '{fid}' is not registered")

        feed = json.loads(raw_feed)
        url = str(feed["url"])
        instruction = str(feed["instruction"])
        tolerance_bps = int(feed["tolerance_bps"])

        # Check if an existing report is active and still within challenge window
        raw_report = self.reports.get(fid, None)
        if raw_report is not None:
            existing = json.loads(raw_report)
            status = existing.get("status")
            reported_at = int(existing.get("reported_at", 0))
            challenge_secs = int(feed.get("challenge_secs", 0))
            now = self._get_current_timestamp()
            if status in (self.STATUS_PENDING, self.STATUS_CHALLENGED) and now <= (reported_at + challenge_secs):
                raise gl.vm.UserError(
                    f"Feed '{fid}' has an active report under review (status: {status}, expires at {reported_at + challenge_secs})"
                )

        def leader_fn() -> int:
            return self._extract_metric(url, instruction)

        def validator_fn(leader_res) -> bool:
            # Check 1: Leader proposal must be a valid Return
            if not isinstance(leader_res, getattr(gl.vm, "Return", object)):
                return False

            # Extract proposed leader value
            leader_val = getattr(leader_res, "calldata", leader_res)
            if not isinstance(leader_val, int):
                try:
                    leader_val = int(leader_val)
                except Exception:
                    return False

            # Check 2: Validator independently fetches and extracts
            try:
                my_val = self._extract_metric(url, instruction)
            except Exception:
                return False

            # Check 3: Numeric tolerance band condition:
            # |my_val - leader_val| <= max(|leader_val|, 1) * tolerance_bps / 10,000
            diff = abs(my_val - leader_val)
            base = max(abs(leader_val), 1)
            allowed_divergence = (base * tolerance_bps) // 10000

            return diff <= allowed_divergence

        # Run non-deterministic consensus block
        reported_val = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        now = self._get_current_timestamp()
        report_data = {
            "feed_id": fid,
            "value": int(reported_val),
            "reported_at": now,
            "status": self.STATUS_PENDING,
            "challenger": None
        }

        self.reports[fid] = json.dumps(report_data)
        return int(reported_val)

    @gl.public.write
    def challenge(self, feed_id: str) -> None:
        """
        Challenges a PENDING report during its challenge window.

        Consensus Mechanism:
        - Re-runs extraction with a stricter tolerance (half the feed's original tolerance).
        - If the new agreed value differs from the original report beyond the ORIGINAL
          tolerance band, the report is replaced with the new value and marked CHALLENGED.
        - Otherwise, the original report is validated and kept, preventing frivolous resets.
        """
        fid = (feed_id or "").strip()
        raw_feed = self.feeds.get(fid, None)
        if raw_feed is None:
            raise gl.vm.UserError(f"Feed '{fid}' is not registered")

        raw_report = self.reports.get(fid, None)
        if raw_report is None:
            raise gl.vm.UserError(f"No report exists for feed '{fid}'")

        feed = json.loads(raw_feed)
        report = json.loads(raw_report)

        if report.get("status") != self.STATUS_PENDING:
            raise gl.vm.UserError(
                f"Cannot challenge report: status is '{report.get('status')}', expected '{self.STATUS_PENDING}'"
            )

        now = self._get_current_timestamp()
        reported_at = int(report.get("reported_at", 0))
        challenge_secs = int(feed.get("challenge_secs", 0))
        deadline = reported_at + challenge_secs

        if now > deadline:
            raise gl.vm.UserError(
                f"Challenge window for feed '{fid}' has closed (closed at {deadline}, current time: {now})"
            )

        url = str(feed["url"])
        instruction = str(feed["instruction"])
        original_tolerance_bps = int(feed["tolerance_bps"])
        # Stricter tolerance for challenge verification (half the original)
        stricter_tolerance_bps = max(1, original_tolerance_bps // 2)

        def challenge_leader_fn() -> int:
            return self._extract_metric(url, instruction)

        def challenge_validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, getattr(gl.vm, "Return", object)):
                return False

            leader_val = getattr(leader_res, "calldata", leader_res)
            if not isinstance(leader_val, int):
                try:
                    leader_val = int(leader_val)
                except Exception:
                    return False

            try:
                my_val = self._extract_metric(url, instruction)
            except Exception:
                return False

            diff = abs(my_val - leader_val)
            base = max(abs(leader_val), 1)
            allowed_stricter = (base * stricter_tolerance_bps) // 10000
            return diff <= allowed_stricter

        # Execute dispute verification round
        new_val = gl.vm.run_nondet_unsafe(challenge_leader_fn, challenge_validator_fn)

        original_val = int(report["value"])
        divergence = abs(new_val - original_val)
        allowed_original_divergence = (max(abs(original_val), 1) * original_tolerance_bps) // 10000

        # Replace value if divergence exceeds the original tolerance band
        if divergence > allowed_original_divergence:
            report["value"] = int(new_val)
            report["status"] = self.STATUS_CHALLENGED
            report["challenger"] = self._get_sender()
            self.reports[fid] = json.dumps(report)
        # If divergence is within original tolerance, the original report stands

    @gl.public.write
    def finalize(self, feed_id: str) -> None:
        """
        Finalizes an observation after its challenge window has closed.

        Requirements:
        - Report must be in PENDING or CHALLENGED status.
        - Current timestamp must strictly exceed reported_at + challenge_secs.
        """
        fid = (feed_id or "").strip()
        raw_feed = self.feeds.get(fid, None)
        if raw_feed is None:
            raise gl.vm.UserError(f"Feed '{fid}' is not registered")

        raw_report = self.reports.get(fid, None)
        if raw_report is None:
            raise gl.vm.UserError(f"No report exists for feed '{fid}'")

        feed = json.loads(raw_feed)
        report = json.loads(raw_report)

        status = report.get("status")
        if status not in (self.STATUS_PENDING, self.STATUS_CHALLENGED):
            raise gl.vm.UserError(
                f"Report for feed '{fid}' cannot be finalized: current status is '{status}'"
            )

        now = self._get_current_timestamp()
        reported_at = int(report.get("reported_at", 0))
        challenge_secs = int(feed.get("challenge_secs", 0))
        deadline = reported_at + challenge_secs

        if now <= deadline:
            raise gl.vm.UserError(
                f"Challenge window is still open for feed '{fid}' (deadline: {deadline}, current time: {now})"
            )

        report["status"] = self.STATUS_FINALIZED
        self.reports[fid] = json.dumps(report)

    # -----------------------------------------------------------------------
    # Public Read-Only Views
    # -----------------------------------------------------------------------

    @gl.public.view
    def get_value(self, feed_id: str) -> int:
        """
        Returns the finalized integer value of a feed.

        Reverts with gl.vm.UserError if:
        - The feed does not exist.
        - No report has been submitted.
        - The report has not yet reached FINALIZED status.
        """
        fid = (feed_id or "").strip()
        raw_report = self.reports.get(fid, None)
        if raw_report is None:
            raise gl.vm.UserError(f"No report exists for feed '{fid}'")

        report = json.loads(raw_report)
        status = report.get("status")
        if status != self.STATUS_FINALIZED:
            raise gl.vm.UserError(
                f"Value for feed '{fid}' is not finalized (status: '{status}')"
            )

        return int(report["value"])

    @gl.public.view
    def get_feed(self, feed_id: str) -> str:
        """Returns JSON metadata for a registered feed."""
        fid = (feed_id or "").strip()
        raw = self.feeds.get(fid, None)
        if raw is None:
            raise gl.vm.UserError(f"Feed '{fid}' does not exist")
        return raw

    @gl.public.view
    def get_report(self, feed_id: str) -> str:
        """Returns JSON metadata for a feed's latest report."""
        fid = (feed_id or "").strip()
        raw = self.reports.get(fid, None)
        if raw is None:
            raise gl.vm.UserError(f"No report exists for feed '{fid}'")
        return raw
