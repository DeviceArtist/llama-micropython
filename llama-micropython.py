#!/usr/bin/env python3
"""
llama-micropython.py
========================
A host-side tool that talks to a MicroPython board over USB (serial REPL)
and lets you "ask Llama" (llama.cpp server) to write MicroPython
code, then run it directly on the device.

Requirements (host):
    pip install pyserial requests

LLM backends (any one, default Ollama):
    llama.cpp:   llama-server -m your-model.gguf --port 8080

Usage:
    python llama-micropython.py                # auto-detect board
    python llama-micropython.py -p COM3        # explicit port (Windows)
    python llama-micropython.py -p /dev/ttyUSB0
    python llama-micropython.py

Interactive commands (inside the shell):
    <anything>            -> executed as MicroPython code on the board
    !ask  <question>      -> ask Llama, show answer (code is NOT run)
    !run  <question>      -> ask Llama, extract code and RUN it on the board
    !exec <file.py>       -> run a local .py file on the board
    !reset                -> soft reset the board (Ctrl-D)
    !hardreset            -> hard reset via machine.reset()
    !raw <code>           -> send code in raw REPL (multi-line safe)
    !help                 -> command list
    !exit                 -> quit

Examples:
    >>> !run blink the onboard LED once per second forever
    >>> !run read temperature every 2 seconds and print it
    >>> import machine
    >>> print(machine.freq())
"""

import argparse
import re
import sys
import time

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

try:
    import requests
except ImportError:
    requests = None  # !ask/!run disabled without it


# --------------------------------------------------------------------------
# Serial / REPL layer
# --------------------------------------------------------------------------
class MicropythonBoard:
    """Controls a MicroPython device over a USB serial REPL."""

    NORMAL_PROMPT = b">>>"
    RAW_OK = b"OK"

    def __init__(self, port, baud=115200, timeout=10):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.timeout = timeout
        time.sleep(0.2)
        self._drain()
        # Wake the REPL: two Ctrl-C then Enter, confirm we see a prompt.
        self.write(b"\r\x03\x03")
        time.sleep(0.1)
        self.write(b"\r\n")
        echo = self.read_until_prompt(b">>>", 3)
        if b">>>" not in echo and b"raw REPL" not in echo:
            raise RuntimeError(
                f"No MicroPython prompt detected on {port}. "
                "Is the board running MicroPython firmware?"
            )

    # -- low level ---------------------------------------------------------
    def write(self, data: bytes):
        self.ser.write(data)
        self.ser.flush()

    def _drain(self):
        while self.ser.in_waiting:
            self.ser.read(self.ser.in_waiting)
            time.sleep(0.02)

    def read_until_prompt(self, tail: bytes, timeout: float) -> bytes:
        """Read until `tail` appears at the end of the stream."""
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser.in_waiting:
                buf += self.ser.read(self.ser.in_waiting)
                if buf.rstrip().endswith(tail.rstrip()):
                    break
            else:
                time.sleep(0.02)
        return buf

    # -- normal REPL -------------------------------------------------------
    def exec_line(self, line: str) -> str:
        """Execute a single statement through the normal REPL."""
        self._drain()
        self.write(line.encode() + b"\r\n")
        out = self.read_until_prompt(self.NORMAL_PROMPT, self.timeout)
        # Strip the echoed command and trailing prompt for clean output.
        out = out.replace(self.NORMAL_PROMPT, b"")
        return out.decode(errors="replace").strip()

    def soft_reset(self) -> str:
        self.write(b"\r\x03\x04")
        out = self.read_until_prompt(b">>>", 5)
        return out.decode(errors="replace").strip()

    def hard_reset(self):
        self.exec_line("import machine; machine.reset()")

    # -- raw REPL (reliable multi-line exec, like mpremote) -----------------
    def exec_raw(self, code: str, timeout=None) -> str:
        """Run arbitrary (multi-line) code via raw REPL; raises on exception."""
        timeout = timeout or self.timeout
        self._drain()
        self.write(b"\x01")  # ctrl-A: enter raw REPL
        ready = self.read_until_prompt(b">", 3)
        if b"raw REPL" not in ready:
            self.write(b"\x02")  # ctrl-B back to normal mode
            raise RuntimeError("Could not enter raw REPL")

        self.write(code.encode() + b"\x04")  # code + ctrl-D = execute
        out, err = b"", b""
        deadline = time.time() + timeout
        state = 0  # 0=stdout, 1=stderr, 2=done
        while time.time() < deadline and state < 3:
            if self.ser.in_waiting:
                data = self.ser.read(self.ser.in_waiting)
                out += data
                if state == 0 and out.endswith(b"\x04"):
                    out = out[:-1]
                    state = 1
                    err = b""
                elif state == 1 and err.endswith(b"\x04"):
                    err = err[:-1]
                    state = 2
                elif state == 2 and out.endswith(b"OK"):
                    out = out[:-2]
                    state = 3
            else:
                time.sleep(0.02)
        self.write(b"\x02")  # ctrl-B: back to normal REPL
        self.read_until_prompt(b">>>", 3)

        if err:
            raise RuntimeError(err.decode(errors="replace").strip())
        return out.decode(errors="replace").rstrip("\r\n> ")

    # -- file upload --------------------------------------------------------
    def upload(self, local_path: str, remote_name: str):
        with open(local_path, "rb") as f:
            content = f.read()
        self.exec_line("import __rawup__")
        # Stream the file in chunks using the special "escape" trick.
        self.write(b"\x05")  # ctrl-E: paste mode
        self.read_until_prompt(b"=== ", 3)
        self.write(
            b"import sys\n"
            b"with open('" + remote_name.encode() + b"','wb') as f:\n"
            b"    f.write(__rawup__.data)\n"
        )
        self.write(b"\x04")  # end paste
        self.read_until_prompt(b">>>", 5)
        self.exec_line("del __rawup__")

    def close(self):
        try:
            self.write(b"\r\x02")  # ctrl-B, ensure normal mode
            self.ser.close()
        except Exception:
            pass


def autodetect_port():
    """Guess the MicroPython board's serial port."""
    ports = list(serial.tools.list_ports.comports())
    # Prefer ports advertising MicroPython, then generic USB-serial chips.
    def score(p):
        desc = (p.description or "").lower()
        if "micropython" in desc or "pyboard" in desc:
            return 0
        if any(k in desc for k in ("cp210", "ch340", "usb serial", "uart", "stm32")):
            return 1
        return 2
    for p in sorted(ports, key=score):
        print(f"  found: {p.device}  ({p.description})")
    if not ports:
        return None
    best = sorted(ports, key=score)[0]
    return best.device


# --------------------------------------------------------------------------
# Llama (LLM) layer
# --------------------------------------------------------------------------
class Llama:
    """Minimal client for  llama.cpp / OpenAI-compatible servers."""

    SYSTEM = (
        "You are a MicroPython expert. The user is controlling a MicroPython "
        "board (ESP32/ESP8266/rp2040) over USB. Answer "
        "concisely. When you provide code, put it in ONE fenced code block "
        "marked ```python, using only modules available in MicroPython "
        "(machine, network, time, utime, etc.)."
    )

    def __init__(self, backend="openai", model=None, url=None, temperature=0.2):
        self.backend = backend
        self.temperature = temperature
        self.url = url or "http://localhost:8080/v1/chat/completions"
        self.model = model or "local"

    def ask(self, question: str, history=None) -> str:
        if requests is None:
            raise RuntimeError("'requests' not installed: pip install requests")
        messages = [{"role": "system", "content": self.SYSTEM}]
        if history:
            messages += history
        messages.append({"role": "user", "content": question})

        url = "http://127.0.0.1:8080/v1/chat/completions"  # 建议使用 127.0.0.1 避免 IPv6 解析问题
        model_name = "default-model"  # 如果 llama-server 未指定模型名，可留空或填任意值
        payload = {
            "model": model_name,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature
        }

        try:
            print(f"connecting {url} ...")
            r = requests.post(url, json=payload, timeout=300)
            r.raise_for_status()  # 如果状态码不是 2xx，抛出 HTTPError
                
            response_data = r.json()
            content = response_data["choices"][0]["message"]["content"]
            print("answer:", content)
            return content
                
        except Exception as e:
            print(f"error: {e}")
            sys.exit(1)


CODE_BLOCK_RE = re.compile(r"```(?:python|micropython)?\s*\n(.*?)```", re.S)


def extract_code(text: str):
    """Return the first fenced code block found in an LLM answer."""
    m = CODE_BLOCK_RE.search(text)
    return m.group(1).strip() if m else None


# --------------------------------------------------------------------------
# Interactive shell
# --------------------------------------------------------------------------
HELP = """
!ask  <question>    ask Llama (answer shown, code NOT executed)
!run  <question>    ask Llama -> extract code -> RUN on the board
!exec <file.py>     run a local python file on the board
!reset              soft reset (Ctrl-D)
!hardreset          hard reset (machine.reset)
!raw  <code>        run multi-line code via raw REPL
!help               this help
!exit               quit
"""


def shell(board: MicropythonBoard, llama: Llama):
    history = []
    print("\nConnected. Type MicroPython code, or !help for commands.\n")
    while True:
        try:
            line = input("board> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        # ---- meta commands -------------------------------------------------
        if line in ("!exit", "!quit", "exit", "quit"):
            break
        elif line in ("!help", "?"):
            print(HELP)
        elif line == "!backend":
            print(f"  backend={llama.backend}  model={llama.model}  url={llama.url}")
        elif line == "!reset":
            print(board.soft_reset() or "(board reset)")
        elif line == "!hardreset":
            print("  resetting board...")
            board.hard_reset()
            time.sleep(1)
        elif line.startswith("!raw "):
            code = line[5:]
            try:
                print(board.exec_raw(code))
            except RuntimeError as e:
                print(f"  error: {e}")
        elif line.startswith("!exec "):
            path = line[6:].strip()
            try:
                with open(path) as f:
                    print(board.exec_raw(f.read(), timeout=60) or "(done)")
            except (OSError, RuntimeError) as e:
                print(f"  error: {e}")
        elif line.startswith("!ask ") or line.startswith("!run "):
            do_run = line.startswith("!run ")
            question = line[5:].strip()
            print("  ... asking llama ...")
            try:
                answer = llama.ask(question, history)
            except Exception as e:
                print(f"  LLM error: {e}")
                continue
            print("-" * 60)
            print(answer)
            print("-" * 60)
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer})
            if len(history) > 8:  # keep context small
                del history[:2]
            if do_run:
                code = extract_code(answer)
                if not code:
                    print("  (no code block found in answer)")
                else:
                    try:
                        print("  running on board ...")
                        out = board.exec_raw(code, timeout=60)
                        print(out or "(code finished)")
                    except RuntimeError as e:
                        print(f"  board error:\n{e}")
        # ---- plain MicroPython line -----------------------------------------
        else:
            try:
                out = board.exec_line(line)
                if out:
                    print(out)
            except serial.SerialException as e:
                print(f"  serial error: {e}")
                break

    board.close()
    print("bye!")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Control a MicroPython board over USB and ask Llama for code.")
    ap.add_argument("-p", "--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("-b", "--baud", type=int, default=115200)
    ap.add_argument("--model", default=None, help="model name (e.g. llama3.2)")
    ap.add_argument("--url", default=None, help="LLM server URL override")
    ap.add_argument("--ask", metavar="QUESTION", help="ask once, run returned code, then exit")
    args = ap.parse_args()

    port = args.port or autodetect_port()
    if not port:
        sys.exit("No serial port found. Plug in the board and try again (or use -p).")
    print(f"Opening {port} @ {args.baud} ...")
    try:
        board = MicropythonBoard(port, args.baud)
    except Exception as e:
        sys.exit(f"Could not open {port}: {e}")

    llama = Llama(args.model, args.url)

    if args.ask:  # one-shot mode: ask llama -> run code -> exit
        print("  ... asking llama ...")
        answer = llama.ask(args.ask)
        print(answer)
        code = extract_code(answer)
        if code:
            try:
                out = board.exec_raw(code, timeout=120)
                print(out or "(code finished)")
            except RuntimeError as e:
                print(f"board error: {e}")
        board.close()
    else:
        shell(board, llama)


if __name__ == "__main__":
    main()
