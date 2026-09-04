"""Driver capacity probe: how much load the generator itself can produce.

The question is NOT how fast a stack replies. It is whether the generator,
with the cpu and memory it has been given, can offer the load a capacity run
needs — because a saturated driver and a saturated stack look alike from the
outside, and only one of them is a finding.

Two roles, one file.

**The target** (``--serve PORT``) answers every request at once. It is one
asyncio event loop in ONE thread: no thread per request, no thread per
connection, so its own cost stays small and, more to the point, stays
MEASURED. It speaks HTTP/1.1 and keeps connections open — it never closes one
itself — so the driver pays for a handshake once per caller and not once per
call, which is what a browser does and what the stacks do. It reports its own
cpu seconds and peak memory from ``resource``, plus how many connections it
accepted and how many requests rode on them: requests-per-connection near one
means the keep-alive is NOT working and the measurement is about handshakes.

**The generator** (``--drive``) opens ``--users`` kept-alive connections, each
posting a body of the same shape and size as the recorded tour's, one every
``--period`` seconds. The offered rate is users/period and it is SCHEDULED: a
call that cannot leave on time is late, and the lateness is the whole point.

What a window reports: how many calls the timetable asked for, how many left,
the lateness at the median and the ninety-fifth, and the errors. A generator
that is not the bottleneck sends what it scheduled, keeps its lateness low and
FLAT, and stays under its cpu ceiling. One that is sends fewer than it
scheduled, or sends them ever later.
"""

import argparse
import asyncio
import http.client
import json
import os
import resource
import statistics
import threading
import time
import urllib.parse

# A form of the same shape and size as the tour's heaviest call: the driver
# pays the real urlencode cost, not the cost of a toy request.
CALL_FORM = {
    "method": "app.getSelection", "table": "adm.user",
    "where": "@username LIKE :lookup", "queryMode": "S",
    "sortedBy": "username", "selectionName": "*V_adm_user_probe",
    "recordResolver": "false::B", "sqlContextName": "standard_list",
    "totalRowCount": "false::B", "row_start": "0",
    "excludeLogicalDeleted": "true::B", "excludeDraft": "true::B",
    "columns": "$username,$firstname,$lastname,$email,$status,$auth_tags",
    "checkPermissions": "true::B", "row_count": "50::L", "storepath": ".store",
    "page_id": "0123456789abcdefghijkl", "callcounter": "100",
}

ANSWER_BODY = b'{"ok":true}'
ANSWER_HEAD = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(ANSWER_BODY)).encode()
               + b"\r\nConnection: keep-alive\r\n\r\n")


class ProcessCost:
    """This process's own cpu and memory, read from the kernel, not guessed."""

    def __init__(self):
        self.started_wall = time.monotonic()
        self.started_cpu = self.cpu_seconds

    @property
    def cpu_seconds(self):
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    @property
    def peak_memory_mb(self):
        """ru_maxrss is kilobytes on Linux and bytes on macOS."""
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if peak > 10 ** 7 else 1024), 1)

    @property
    def cpu_percent(self):
        """Cpu burned since the start, as a share of ONE core."""
        elapsed = time.monotonic() - self.started_wall
        if elapsed <= 0:
            return 0.0
        return round(100.0 * (self.cpu_seconds - self.started_cpu) / elapsed, 1)

    def take_interval(self):
        """Cpu percent since the previous call, then restart the interval."""
        percent = self.cpu_percent
        self.started_wall = time.monotonic()
        self.started_cpu = self.cpu_seconds
        return percent


class Target:
    """The bersaglio: one event loop, one thread, connections held open."""

    def __init__(self, arguments):
        self.arguments = arguments
        self.cost = ProcessCost()
        self.connections = 0
        self.open_connections = 0
        self.requests = 0

    async def handle_connection(self, reader, writer):
        """Serve requests on ONE connection until the caller goes away."""
        self.connections += 1
        self.open_connections += 1
        try:
            while True:
                length = 0
                while True:
                    line = await reader.readline()
                    if not line:
                        return                      # the caller closed
                    if line in (b"\r\n", b"\n"):
                        break
                    if line[:15].lower() == b"content-length:":
                        length = int(line.split(b":", 1)[1])
                if length:
                    await reader.readexactly(length)
                writer.write(ANSWER_HEAD + ANSWER_BODY)
                await writer.drain()
                self.requests += 1
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            return
        finally:
            self.open_connections -= 1
            writer.close()

    async def report_forever(self):
        """Say what it cost and what rode on the connections, every window."""
        previous_requests = 0
        while True:
            await asyncio.sleep(self.arguments.window)
            served = self.requests - previous_requests
            previous_requests = self.requests
            per_connection = (self.requests / self.connections) if self.connections else 0
            print(f"  target: {served / self.arguments.window:.0f} req/s, "
                  f"{self.connections} connections ({self.open_connections} open), "
                  f"{per_connection:.1f} req/connection, "
                  f"cpu {self.cost.take_interval()}% of one core, "
                  f"peak mem {self.cost.peak_memory_mb} MB", flush=True)

    async def serve(self):
        server = await asyncio.start_server(
            self.handle_connection, "0.0.0.0", self.arguments.serve)
        print(f"target listening on {self.arguments.serve} "
              f"(pid {os.getpid()}, one thread, HTTP/1.1 keep-alive)", flush=True)
        asyncio.create_task(self.report_forever())
        async with server:
            await server.serve_forever()


class Generator:
    """One emulated caller: its own kept-alive connection, its own timetable."""

    def __init__(self, probe, number):
        self.probe = probe
        self.number = number
        self.connection = http.client.HTTPConnection(probe.host, probe.port, timeout=10)
        self.reconnections = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.generate_traffic, daemon=True)

    def start(self):
        self.thread.start()

    def generate_traffic(self):
        """Send on the timetable, and record how late each departure was."""
        period = self.probe.arguments.period
        # departures are spread across the period, as arrivals would be
        started = time.monotonic() + period * (self.number / max(1, self.probe.arguments.users))
        counter = 0
        while not self.stop_event.is_set():
            due = started + counter * period
            now = time.monotonic()
            if due > now:
                if self.stop_event.wait(due - now):
                    return
                lateness = 0.0
            else:
                lateness = now - due
            self.send_call(lateness)
            counter += 1

    def send_call(self, lateness):
        """One call on the standing connection; reopen it only if it broke."""
        body = urllib.parse.urlencode(dict(CALL_FORM, callcounter=str(int(lateness * 1000))))
        failed = False
        try:
            self.connection.request("POST", "/", body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
            answer = self.connection.getresponse()
            answer.read()
            failed = answer.status != 200
        except Exception:
            failed = True
            self.reconnections += 1
            try:
                self.connection.close()
            except Exception:
                pass
            self.connection = http.client.HTTPConnection(
                self.probe.host, self.probe.port, timeout=10)
        self.probe.record_call(lateness, failed)


class CapacityProbe:
    """The whole probe: the generators, the windows, the verdict."""

    def __init__(self, arguments):
        self.arguments = arguments
        parsed = urllib.parse.urlparse(arguments.target)
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.cost = ProcessCost()
        self.lock = threading.Lock()
        self.late = []
        self.sent = 0
        self.errors = 0
        self.windows = []
        self.generators = []

    @property
    def offered_rate(self):
        """Calls per second the timetable asks for."""
        return self.arguments.users / self.arguments.period

    @property
    def reconnections(self):
        """How many times a caller had to open a new connection."""
        return sum(generator.reconnections for generator in self.generators)

    def record_call(self, lateness, failed):
        with self.lock:
            self.sent += 1
            self.late.append(lateness * 1000.0)
            if failed:
                self.errors += 1

    def take_window(self, seconds):
        with self.lock:
            sent, self.sent = self.sent, 0
            errors, self.errors = self.errors, 0
            late, self.late = sorted(self.late), []
        scheduled = self.offered_rate * seconds
        return {
            "scheduled": round(scheduled),
            "sent": sent,
            "sent_percent": round(100.0 * sent / scheduled, 1) if scheduled else 0.0,
            "errors": errors,
            "late_p50_ms": self.get_percentile(late, 50),
            "late_p95_ms": self.get_percentile(late, 95),
            "driver_cpu_percent": self.cost.take_interval(),
            "driver_peak_mb": self.cost.peak_memory_mb,
            "reconnections": self.reconnections,
        }

    def get_percentile(self, values, which):
        if not values:
            return 0.0
        if len(values) == 1:
            return round(values[0], 1)
        return round(statistics.quantiles(values, n=100)[which - 1], 1)

    def run(self):
        print(f"probe: {self.arguments.users} callers, one call every "
              f"{self.arguments.period}s each = {self.offered_rate:.0f} calls/s offered, "
              f"for {self.arguments.seconds:.0f}s", flush=True)
        self.generators = [Generator(self, number) for number in range(self.arguments.users)]
        for generator in self.generators:
            generator.start()
        window = self.arguments.window
        deadline = time.monotonic() + self.arguments.seconds
        time.sleep(window)                            # let every caller get going
        self.take_window(window)                      # and discard that first stretch
        while time.monotonic() < deadline:
            time.sleep(window)
            summary = self.take_window(window)
            self.windows.append(summary)
            print(f"  driver: sent {summary['sent']}/{summary['scheduled']} "
                  f"({summary['sent_percent']}%), late p50 {summary['late_p50_ms']} ms "
                  f"p95 {summary['late_p95_ms']} ms, errors {summary['errors']}, "
                  f"cpu {summary['driver_cpu_percent']}% of one core, "
                  f"reconnections {summary['reconnections']}", flush=True)
        for generator in self.generators:
            generator.stop_event.set()
        self.print_verdict()

    def print_verdict(self):
        """PASS only if every window sent what it scheduled, without drifting."""
        if not self.windows:
            print("no windows: run longer than one window")
            return
        worst_sent = min(window["sent_percent"] for window in self.windows)
        worst_late = max(window["late_p95_ms"] for window in self.windows)
        errors = sum(window["errors"] for window in self.windows)
        peak_cpu = max(window["driver_cpu_percent"] for window in self.windows)
        # The drift is read between the two HALVES of the run, not between the
        # first and last window: one window is noise, half a run is a trend.
        half = max(1, len(self.windows) // 2)
        first_late = self.get_percentile(
            sorted(window["late_p95_ms"] for window in self.windows[:half]), 50)
        last_late = self.get_percentile(
            sorted(window["late_p95_ms"] for window in self.windows[-half:]), 50)
        drifting = last_late > first_late * 1.3 and last_late - first_late > 200.0
        print()
        print(f"offered rate:          {self.offered_rate:.0f} calls/s")
        print(f"worst window sent:     {worst_sent}% of scheduled")
        print(f"worst lateness p95:    {worst_late} ms")
        print(f"lateness 1st->2nd half:{first_late} -> {last_late} ms"
              f"  ({'DRIFTING' if drifting else 'flat'})")
        print(f"driver cpu peak:       {peak_cpu}% of one core")
        print(f"driver peak memory:    {self.cost.peak_memory_mb} MB")
        print(f"reconnections:         {self.reconnections}")
        print(f"driver errors:         {errors}")
        passed = worst_sent >= 99.0 and not drifting and errors == 0
        print(f"VERDICT: {'PASS' if passed else 'FAIL'} — the driver "
              f"{'can' if passed else 'CANNOT'} offer this rate")
        if self.arguments.json:
            with open(self.arguments.json, "w") as handle:
                json.dump({"offered_rate": self.offered_rate,
                           "windows": self.windows,
                           "peak_cpu_percent": peak_cpu,
                           "peak_memory_mb": self.cost.peak_memory_mb,
                           "reconnections": self.reconnections,
                           "passed": passed}, handle, indent=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int,
                        help="run as the target on this port")
    parser.add_argument("--drive", action="store_true", help="run as the generator")
    parser.add_argument("--target", default="http://172.17.0.1:8097",
                        help="where the target listens, as seen from the driver")
    parser.add_argument("--users", type=int, default=280)
    parser.add_argument("--period", type=float, default=0.35,
                        help="seconds between one caller's calls; users/period = offered rate")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--window", type=float, default=10.0)
    parser.add_argument("--json", help="write the windows and the verdict here")
    arguments = parser.parse_args()
    if arguments.serve:
        asyncio.run(Target(arguments).serve())
    elif arguments.drive:
        CapacityProbe(arguments).run()
    else:
        parser.error("choose --serve PORT or --drive")


if __name__ == "__main__":
    main()
