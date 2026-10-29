#!/usr/bin/env bash
# Egress audit for Clara: run a Clara command and record every network destination
# of its whole process tree, independently of Clara's own guard.
#
#   scripts/netaudit.sh -- clara -p "list the files"
#   sudo scripts/netaudit.sh --pcap -- clara            # also a packet capture for Wireshark
#   scripts/netaudit.sh --allow-lan 192.168.178.0/24 -- clara serve --lan
#
# Three independent recorders, each optional, each reported separately:
#   strace   every connect()/sendto()/sendmsg() of the process tree with the address (no root needed)
#   tcpdump  packet capture on all interfaces, loopback excluded (--pcap, needs root; open in Wireshark)
#   ss       socket table sampled every second, limited to the process tree (fallback when strace is missing)
# Verdict: PASS when no recorder saw a destination outside loopback (and the allowed LAN, if given).
set -u
ALLOW_LAN=""
PCAP=0
OUT="${CLARA_NETAUDIT_DIR:-$HOME/.clara/netaudit-runs}"
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-lan) ALLOW_LAN="$2"; shift 2 ;;
    --pcap) PCAP=1; shift ;;
    --out) OUT="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
if [ $# -eq 0 ]; then echo "usage: $0 [--allow-lan CIDR] [--pcap] [--out DIR] -- clara [args]" >&2; exit 2; fi
mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M%S)
RUN="$OUT/$STAMP"
mkdir -p "$RUN"
echo "netaudit: recording into $RUN"

HAVE_STRACE=0; command -v strace >/dev/null 2>&1 && HAVE_STRACE=1
HAVE_TCPDUMP=0; command -v tcpdump >/dev/null 2>&1 && HAVE_TCPDUMP=1
HAVE_SS=0; command -v ss >/dev/null 2>&1 && HAVE_SS=1
TCPDUMP_PID=""
if [ "$PCAP" = 1 ]; then
  if [ "$HAVE_TCPDUMP" = 1 ] && [ "$(id -u)" = 0 ]; then
    # Everything except loopback. The phone's LAN traffic will be in here; the verdict filters it.
    tcpdump -i any -n -U -w "$RUN/capture.pcap" 'not (src net 127.0.0.0/8 or dst net 127.0.0.0/8 or src host ::1 or dst host ::1)' >"$RUN/tcpdump.log" 2>&1 &
    TCPDUMP_PID=$!
    sleep 1
    echo "netaudit: tcpdump running (pid $TCPDUMP_PID)"
  else
    echo "netaudit: --pcap needs tcpdump and root; skipping the packet capture" >&2
  fi
fi

# ---- run the command under strace (or plainly) and sample ss in the background
if [ "$HAVE_STRACE" = 1 ]; then
  strace -f -qq -e trace=connect,sendto,sendmsg -e signal=none -s 200 -o "$RUN/strace.log" "$@" </dev/stdin
  RC=$?
  echo "netaudit: command exit code $RC"
else
  echo "netaudit: strace not installed (apt install strace); only the socket table will be sampled" >&2
  "$@" </dev/stdin &
  MAIN=$!
  if [ "$HAVE_SS" = 1 ]; then
    : > "$RUN/ss.log"
    while kill -0 "$MAIN" 2>/dev/null; do
      ss -tunapH 2>/dev/null | grep -E "pid=($MAIN|$(pgrep -P "$MAIN" | paste -sd'|' -))," >> "$RUN/ss.log"
      sleep 1
    done
  fi
  wait "$MAIN"; RC=$?
  echo "netaudit: command exit code $RC"
fi
if [ -n "$TCPDUMP_PID" ]; then sleep 1; kill -INT "$TCPDUMP_PID" 2>/dev/null; wait "$TCPDUMP_PID" 2>/dev/null; fi

# ---- verdict
python3 - "$RUN" "$ALLOW_LAN" <<'PY'
import ipaddress, re, sys, os
run, allow = sys.argv[1], sys.argv[2]
nets = [ipaddress.ip_network(allow)] if allow else []
def external(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified:
        return False
    return not any(a in n for n in nets)
seen = {}
p = os.path.join(run, "strace.log")
if os.path.exists(p):
    rx = re.compile(r'sin6?_addr=inet6?_addr\("([^"]+)"\)|sin6?_port=htons\((\d+)\)')
    for line in open(p, errors="replace"):
        ips = re.findall(r'inet6?_addr\("([^"]+)"\)', line)
        ports = re.findall(r'htons\((\d+)\)', line)
        for ip in ips:
            key = (ip, ports[0] if ports else "?")
            seen[key] = seen.get(key, 0) + 1
    ext = {k: v for k, v in seen.items() if external(k[0])}
    print(f"strace: {sum(seen.values())} address operations, {len(seen)} distinct destinations")
    for (ip, port), n in sorted(seen.items()):
        print(f"   {'EXTERNAL' if external(ip) else 'local   '} {ip}:{port} x{n}")
    print("strace verdict:", "FAIL (external destinations above)" if ext else "PASS (only loopback" + (" and allowed LAN" if nets else "") + ")")
else:
    print("strace: not available for this run")
p = os.path.join(run, "ss.log")
if os.path.exists(p):
    peers = set()
    for line in open(p, errors="replace"):
        parts = line.split()
        if len(parts) >= 6:
            peer = parts[5].rsplit(":", 1)[0].strip("[]")
            if external(peer):
                peers.add(parts[5])
    print("ss verdict:", f"FAIL external peers {sorted(peers)}" if peers else "PASS (no external peer in the sampled socket table)")
p = os.path.join(run, "capture.pcap")
if os.path.exists(p):
    print(f"pcap: {p} ({os.path.getsize(p)} bytes). Open in Wireshark; display filter in scripts/wireshark-filter.txt.")
    print("      Note: the capture is machine-wide; other programs' packets are in it too. strace above is per process tree.")
PY
exit "$RC"
