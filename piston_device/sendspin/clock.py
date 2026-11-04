"""The Sendspin time filter: a two-dimensional Kalman filter tracking the clock offset and drift between this device
and the server. A port of the specification (Sendspin/spec messaging.md "Time filter") and its C++ reference
(Sendspin/time-filter @ 531e270). All times are microseconds; the state is double precision.

Where aiosendspin's Python port differs from the spec, this follows the spec: samples with max_error <= 0 are discarded,
conversions round halves away from zero, and the measurement uses integer division truncating toward zero."""
import math

PROCESS_STD_DEV = 0.0
DRIFT_PROCESS_STD_DEV = 1e-11
FORGET_FACTOR = 2.0
ADAPTIVE_CUTOFF = 3.0
MIN_SAMPLES = 100
DRIFT_SIGNIFICANCE_THRESHOLD = 2.0
MAX_ERROR_SCALE = 0.5
BURST_MESSAGES = 8  # the spec's minimum per burst
BURST_INTERVAL_S = 10  # bursts start no more than this far apart


def round_half_away(x: float) -> int:
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _div2(n: int) -> int:
    """Integer division by 2 truncating toward zero (Python's // floors)."""
    return -((-n) // 2) if n < 0 else n // 2


def sample_from_exchange(t1: int, t2: int, t3: int, t4: int) -> tuple:
    """(measurement, max_error, time_added) of one client/time exchange: T1 client transmitted, T2 server received,
    T3 server transmitted, T4 client received."""
    return _div2((t2 - t1) + (t3 - t4)), _div2((t4 - t1) - (t3 - t2)), t4


def best_of_burst(samples) -> tuple | None:
    """The sample with the lowest max_error: the only one the spec lets the filter see from a burst."""
    valid = [s for s in samples if s[1] > 0]
    return min(valid, key=lambda s: s[1]) if valid else None


class ClockFilter:
    def __init__(self, process_std_dev: float = PROCESS_STD_DEV, drift_process_std_dev: float = DRIFT_PROCESS_STD_DEV,
                 forget_factor: float = FORGET_FACTOR, adaptive_cutoff: float = ADAPTIVE_CUTOFF, min_samples: int = MIN_SAMPLES,
                 drift_significance_threshold: float = DRIFT_SIGNIFICANCE_THRESHOLD, max_error_scale: float = MAX_ERROR_SCALE):
        self._process_variance = process_std_dev ** 2
        self._drift_process_variance = drift_process_std_dev ** 2
        self._forget_variance = forget_factor ** 2
        self._cutoff = adaptive_cutoff
        self._min_samples = min_samples
        self._drift_threshold_sq = drift_significance_threshold ** 2
        self._max_error_scale = max_error_scale
        self.reset()

    def reset(self) -> None:
        self.count = 0
        self.last_update = 0
        self.offset = 0.0
        self.drift = 0.0
        self.p_oo = math.inf
        self.p_od = 0.0
        self.p_dd = 0.0
        self.use_drift = False

    @property
    def error_us(self) -> int:
        """Standard deviation of the offset estimate (infinite until the first sample)."""
        return round_half_away(math.sqrt(self.p_oo)) if math.isfinite(self.p_oo) else -1

    @property
    def converged(self) -> bool:
        """Ready to schedule playback: at least two samples (the second gives the first drift estimate) and a finite
        offset variance. The spec sets no numeric threshold; this matches aiosendspin's `is_synchronized`."""
        return self.count >= 2 and math.isfinite(self.p_oo)

    def update(self, measurement: int, max_error: int, time_added: int) -> bool:
        """Feed one measurement (see sample_from_exchange). Returns False when it was discarded."""
        if time_added <= self.last_update or max_error <= 0:
            return False
        dt = float(time_added - self.last_update)
        self.last_update = time_added
        r = (self._max_error_scale * max_error) ** 2
        if self.count == 0:
            self.offset, self.p_oo, self.drift, self.count = float(measurement), r, 0.0, 1
            return True
        if self.count == 1:
            self.drift = (measurement - self.offset) / dt
            self.p_dd = (self.p_oo + r) / (dt * dt)
            self.offset, self.p_oo, self.count = float(measurement), r, 2
            return True
        offset_p = self.offset + self.drift * dt
        p_oo_p = self.p_oo + 2 * self.p_od * dt + self.p_dd * dt * dt + self._process_variance * dt
        p_od_p = self.p_od + self.p_dd * dt
        p_dd_p = self.p_dd + self._drift_process_variance * dt
        y = measurement - offset_p
        if self.count < self._min_samples:
            self.count += 1
        elif abs(y) > self._cutoff * max_error:
            p_oo_p *= self._forget_variance
            p_od_p *= self._forget_variance
            p_dd_p *= self._forget_variance
        s = p_oo_p + r
        k_o, k_d = p_oo_p / s, p_od_p / s
        self.offset = offset_p + k_o * y
        self.drift = self.drift + k_d * y
        self.p_oo = p_oo_p - k_o * p_oo_p
        self.p_od = p_od_p - k_d * p_oo_p
        self.p_dd = p_dd_p - k_d * p_od_p
        self.use_drift = self.drift ** 2 > self._drift_threshold_sq * self.p_dd
        return True

    def compute_server_time(self, t_client: int) -> int:
        d = self.drift if self.use_drift else 0.0
        return t_client + round_half_away(self.offset + d * (t_client - self.last_update))

    def compute_client_time(self, t_server: int) -> int:
        d = self.drift if self.use_drift else 0.0
        return round_half_away((t_server - self.offset + d * self.last_update) / (1 + d))
