# Micro-controller clock synchronization
#
# Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math
from typing import Optional, Tuple

from klippy.reactor import SelectReactor
from klippy.serialhdl import SerialReader

RTT_AGE = 0.000010 / (60.0 * 60.0)
DECAY = 1.0 / 30.0
TRANSMIT_EXTRA = 0.001


class ClockSync:
    def __init__(self, reactor: SelectReactor):
        self.get_clock_cmd = None
        self.serial: Optional[SerialReader] = None
        self.reactor = reactor
        self.get_clock_timer = reactor.register_timer(self._get_clock_event)
        self.cmd_queue = None
        self.queries_pending: int = 0
        self.mcu_freq: float = 1.0
        self.last_clock: int = 0
        self.clock_est: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        # Minimum round-trip-time tracking
        self.min_half_rtt: float = 999999999.9
        self.min_rtt_time: float = 0.0
        # Linear regression of mcu clock and system sent_time
        self.time_avg: float = 0.0
        self.time_variance: float = 0.0
        self.clock_avg: float = 0.0
        self.clock_covariance: float = 0.0
        self.prediction_variance: float = 0.0
        self.last_prediction_time: float = 0.0

    def connect(self, serial: SerialReader) -> None:
        self.serial = serial
        self.mcu_freq = serial.msgparser.get_constant_float("CLOCK_FREQ")
        # Load initial clock and frequency
        params = serial.send_with_response("get_uptime", "uptime")
        self.last_clock = (params["high"] << 32) | params["clock"]
        self.clock_avg = self.last_clock
        self.time_avg = params["#sent_time"]
        self.clock_est = (self.time_avg, self.clock_avg, self.mcu_freq)
        self.prediction_variance = (0.001 * self.mcu_freq) ** 2
        # Enable periodic get_clock timer
        for _ in range(8):
            self.reactor.pause(self.reactor.monotonic() + 0.050)
            self.last_prediction_time = -9999.0
            params = serial.send_with_response("get_clock", "clock")
            self._handle_clock(params)
        self.get_clock_cmd = serial.get_msgparser().create_command("get_clock")
        self.cmd_queue = serial.alloc_command_queue()
        serial.register_response(self._handle_clock, "clock")
        self.reactor.update_timer(self.get_clock_timer, self.reactor.NOW)

    def connect_file(self, serial: SerialReader, pace: bool = False) -> None:
        self.serial = serial
        self.mcu_freq = serial.msgparser.get_constant_float("CLOCK_FREQ")
        self.clock_est = (0.0, 0.0, self.mcu_freq)
        freq = 1000000000000.0
        if pace:
            freq = self.mcu_freq
        serial.set_clock_est(freq, self.reactor.monotonic(), 0, 0)

    # MCU clock querying (_handle_clock is invoked from background thread)
    def _get_clock_event(self, eventtime: float) -> float:
        self.serial.raw_send(self.get_clock_cmd, 0, 0, self.cmd_queue)
        self.queries_pending += 1
        # Use an unusual time for the next event so clock messages
        # don't resonate with other periodic events.
        return eventtime + 0.9839

    def _handle_clock(self, params) -> None:
        self.queries_pending = 0
        # Extend clock to 64bit
        last_clock = self.last_clock
        clock_delta = (params["clock"] - last_clock) & 0xFFFFFFFF
        self.last_clock = clock = last_clock + clock_delta
        # Check if this is the best round-trip-time seen so far
        sent_time = params["#sent_time"]
        if not sent_time:
            return
        receive_time = params["#receive_time"]
        half_rtt = 0.5 * (receive_time - sent_time)
        aged_rtt = (sent_time - self.min_rtt_time) * RTT_AGE
        if half_rtt < self.min_half_rtt + aged_rtt:
            self.min_half_rtt = half_rtt
            self.min_rtt_time = sent_time
            logging.debug(
                "new minimum rtt %.3f: hrtt=%.6f freq=%d",
                sent_time,
                half_rtt,
                self.clock_est[2],
            )
        # Filter out samples that are extreme outliers
        exp_clock = (sent_time - self.time_avg) * self.clock_est[2] + self.clock_avg
        clock_diff2 = (clock - exp_clock) ** 2
        if (
            clock_diff2 > 25.0 * self.prediction_variance
            and clock_diff2 > (0.000500 * self.mcu_freq) ** 2
        ):
            if clock > exp_clock and sent_time < self.last_prediction_time + 10.0:
                logging.debug(
                    "Ignoring clock sample %.3f:" " freq=%d diff=%d stddev=%.3f",
                    sent_time,
                    self.clock_est[2],
                    clock - exp_clock,
                    math.sqrt(self.prediction_variance),
                )
                return
            logging.info(
                "Resetting prediction variance %.3f:" " freq=%d diff=%d stddev=%.3f",
                sent_time,
                self.clock_est[2],
                clock - exp_clock,
                math.sqrt(self.prediction_variance),
            )
            self.prediction_variance = (0.001 * self.mcu_freq) ** 2
        else:
            self.last_prediction_time = sent_time
            self.prediction_variance = (1.0 - DECAY) * (
                self.prediction_variance + clock_diff2 * DECAY
            )
        # Add clock and sent_time to linear regression
        diff_sent_time = sent_time - self.time_avg
        self.time_avg += DECAY * diff_sent_time
        self.time_variance = (1.0 - DECAY) * (
            self.time_variance + diff_sent_time**2 * DECAY
        )
        diff_clock = clock - self.clock_avg
        self.clock_avg += DECAY * diff_clock
        self.clock_covariance = (1.0 - DECAY) * (
            self.clock_covariance + diff_sent_time * diff_clock * DECAY
        )
        # Update prediction from linear regression
        new_freq = self.clock_covariance / self.time_variance
        pred_stddev = math.sqrt(self.prediction_variance)
        self.serial.set_clock_est(
            new_freq,
            self.time_avg + TRANSMIT_EXTRA,
            int(self.clock_avg - 3.0 * pred_stddev),
            clock,
        )
        self.clock_est = (self.time_avg + self.min_half_rtt, self.clock_avg, new_freq)
        # logging.debug("regr %.3f: freq=%.3f d=%d(%.3f)",
        #              sent_time, new_freq, clock - exp_clock, pred_stddev)

    # clock frequency conversions
    def print_time_to_clock(self, print_time) -> int:
        return int(print_time * self.mcu_freq)

    def clock_to_print_time(self, clock) -> float:
        return clock / self.mcu_freq

    # system time conversions
    def get_clock(self, eventtime: float) -> int:
        sample_time, clock, freq = self.clock_est
        return int(clock + (eventtime - sample_time) * freq)

    def estimate_clock_systime(self, reqclock) -> float:
        sample_time, clock, freq = self.clock_est
        return float(reqclock - clock) / freq + sample_time

    def estimated_print_time(self, eventtime):
        return self.clock_to_print_time(self.get_clock(eventtime))

    # misc commands
    def clock32_to_clock64(self, clock32):
        last_clock = self.last_clock
        clock_diff = (clock32 - last_clock) & 0xFFFFFFFF
        clock_diff -= (clock_diff & 0x80000000) << 1
        return last_clock + clock_diff

    def is_active(self) -> bool:
        return self.queries_pending <= 4

    def dump_debug(self) -> str:
        sample_time, clock, freq = self.clock_est
        return (
            f"clocksync state: mcu_freq={self.mcu_freq}"
            f" last_clock={self.last_clock}"
            f" clock_est=({sample_time:.3f} {clock} {freq:.3f})"
            f" min_half_rtt={self.min_half_rtt:.6f}"
            f" min_rtt_time={self.min_rtt_time:.3f}"
            f" time_avg={self.time_avg:.3f}({self.time_variance:.3f})"
            f" clock_avg={self.clock_avg:.3f}({self.clock_covariance:.3f})"
            f" pred_variance={self.prediction_variance:.3f}"
        )

    def stats(self, eventtime: float) -> str:
        sample_time, clock, freq = self.clock_est
        return f"freq={freq}"

    def calibrate_clock(self, print_time, eventtime) -> Tuple[float, float]:
        return (0.0, self.mcu_freq)


# Clock syncing code for secondary MCUs (whose clocks are sync'ed to a
# primary MCU)
class SecondarySync(ClockSync):
    def __init__(self, reactor: SelectReactor, main_sync: ClockSync):
        ClockSync.__init__(self, reactor)
        self.main_sync: ClockSync = main_sync
        self.clock_adj: Tuple[float, float] = (0.0, 1.0)
        self.last_sync_time: float = 0.0

    def connect(self, serial: SerialReader) -> None:
        ClockSync.connect(self, serial)
        self.clock_adj = (0.0, self.mcu_freq)
        curtime = self.reactor.monotonic()
        main_print_time = self.main_sync.estimated_print_time(curtime)
        local_print_time = self.estimated_print_time(curtime)
        self.clock_adj = (main_print_time - local_print_time, self.mcu_freq)
        self.calibrate_clock(0.0, curtime)

    def connect_file(self, serial: SerialReader, pace: bool = False) -> None:
        ClockSync.connect_file(self, serial, pace)
        self.clock_adj = (0.0, self.mcu_freq)

    # clock frequency conversions
    def print_time_to_clock(self, print_time: float) -> int:
        adjusted_offset, adjusted_freq = self.clock_adj
        return int((print_time - adjusted_offset) * adjusted_freq)

    def clock_to_print_time(self, clock: float) -> float:
        adjusted_offset, adjusted_freq = self.clock_adj
        return clock / adjusted_freq + adjusted_offset

    # misc commands
    def dump_debug(self) -> str:
        adjusted_offset, adjusted_freq = self.clock_adj
        return f"{ClockSync.dump_debug(self)} clock_adj=({adjusted_offset:.3f} {adjusted_freq:.3f})"

    def stats(self, eventtime: float) -> str:
        adjusted_offset, adjusted_freq = self.clock_adj
        return f"{ClockSync.stats(self, eventtime)} adj={adjusted_freq}"

    def calibrate_clock(
        self, print_time: float, eventtime: float
    ) -> Tuple[float, float]:
        # Calculate: est_print_time = main_sync.estimatated_print_time()
        ser_time, ser_clock, ser_freq = self.main_sync.clock_est
        main_mcu_freq = self.main_sync.mcu_freq
        est_main_clock = (eventtime - ser_time) * ser_freq + ser_clock
        est_print_time = est_main_clock / main_mcu_freq
        # Determine sync1_print_time and sync2_print_time
        sync1_print_time = max(print_time, est_print_time)
        sync2_print_time = max(
            sync1_print_time + 4.0,
            self.last_sync_time,
            print_time + 2.5 * (print_time - est_print_time),
        )
        # Calc sync2_sys_time (inverse of main_sync.estimatated_print_time)
        sync2_main_clock = sync2_print_time * main_mcu_freq
        sync2_sys_time = ser_time + (sync2_main_clock - ser_clock) / ser_freq
        # Adjust freq so estimated print_time will match at sync2_print_time
        sync1_clock = self.print_time_to_clock(sync1_print_time)
        sync2_clock = self.get_clock(sync2_sys_time)
        adjusted_freq = (sync2_clock - sync1_clock) / (
            sync2_print_time - sync1_print_time
        )
        adjusted_offset = sync1_print_time - sync1_clock / adjusted_freq
        # Apply new values
        self.clock_adj = (adjusted_offset, adjusted_freq)
        self.last_sync_time = sync2_print_time
        return self.clock_adj
