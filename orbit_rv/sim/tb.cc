// CXXRTL testbench for the orbit-rv SoC.
//
// Models main memory on the SoC's RAM bus (1-cycle acknowledge, like the FPGA
// block RAM), decodes the UART TX line bit by bit, drives scripted input on
// the RX line, writes the core's commit trace and stops on the finisher,
// a tohost write, or a cycle limit. Counters are written as JSON.
#include "soc.cc"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <string>
#include <vector>

using namespace std;

static vector<uint8_t> read_file(const char *path) {
  vector<uint8_t> d;
  FILE *f = fopen(path, "rb");
  if (!f) { fprintf(stderr, "cannot open %s\n", path); exit(2); }
  uint8_t buf[65536];
  size_t n;
  while ((n = fread(buf, 1, sizeof buf, f)) > 0) d.insert(d.end(), buf, buf + n);
  fclose(f);
  return d;
}

int main(int argc, char **argv) {
  const char *image = nullptr, *log_path = nullptr, *uart_path = nullptr, *script_path = nullptr;
  const char *perf_path = nullptr;
  string prompt = "orbit> ";
  uint64_t ram_size = 16 << 20, max_cycles = 10000000, tohost = 0, entry = 0x80000000ull;
  for (int i = 1; i < argc; i++) {
    string a = argv[i];
    auto next = [&]() { return argv[++i]; };
    if (a == "--image") image = next();
    else if (a == "--ram-size") ram_size = strtoull(next(), 0, 0);
    else if (a == "--max-cycles") max_cycles = strtoull(next(), 0, 0);
    else if (a == "--log") log_path = next();
    else if (a == "--uart") uart_path = next();
    else if (a == "--script") script_path = next();
    else if (a == "--prompt") prompt = next();
    else if (a == "--tohost") tohost = strtoull(next(), 0, 0);
    else if (a == "--entry") entry = strtoull(next(), 0, 0);
    else if (a == "--perf") perf_path = next();
    else { fprintf(stderr, "unknown argument %s\n", a.c_str()); return 2; }
  }
  if (!image) { fprintf(stderr, "--image required\n"); return 2; }
  const uint64_t RAM_BASE = 0x80000000ull;
  vector<uint64_t> ram(ram_size / 8, 0);
  {
    vector<uint8_t> img = read_file(image);
    if (img.size() > ram_size) { fprintf(stderr, "image larger than RAM\n"); return 2; }
    memcpy(ram.data(), img.data(), img.size());
  }
  deque<string> script;
  if (script_path) {
    vector<uint8_t> s = read_file(script_path);
    string cur;
    for (uint8_t c : s) { cur += (char)c; if (c == '\n') { script.push_back(cur); cur.clear(); } }
    if (!cur.empty()) script.push_back(cur);
  }
  FILE *log = log_path ? fopen(log_path, "w") : nullptr;
  FILE *uout = uart_path ? fopen(uart_path, "wb") : nullptr;

  cxxrtl_design::p_top top;
  top.p_uart__rx.set(true);
  top.p_boot__pc.set(entry);
  // reset is implicit (initial values); run one settle step
  top.p_clk.set(false);
  top.step();

  bool ack = false;
  uint64_t rdata = 0;
  // UART TX decoder state
  int tx_state = 0; uint64_t tx_t = 0; int tx_bit = 0; uint8_t tx_byte = 0;
  string out;
  // UART RX driver
  deque<uint8_t> rx_q; bool rx_busy = false; uint64_t rx_t = 0; int rx_bit = 0; uint16_t rx_frame = 0;
  size_t prompt_seen = 0, sent_at = 0;
  string waiting;
  uint64_t commits = 0;
  int exit_code = -1;
  const char *why = "cycle limit";
  uint64_t cyc;
  for (cyc = 0; cyc < max_cycles; cyc++) {
    top.p_ram__ack.set(ack);
    top.p_ram__dat__r.set(rdata);
    uint32_t div = top.p_uart__div.get<uint32_t>();
    if (div == 0) div = 1;
    uint64_t bit_cycles = 16ull * div;
    // RX: shift queued bytes onto the line (8N1)
    if (!rx_busy && !rx_q.empty()) {
      rx_frame = (uint16_t)((rx_q.front() << 1) | 0x200);
      rx_q.pop_front(); rx_busy = true; rx_t = 0; rx_bit = 0;
    }
    if (rx_busy) {
      top.p_uart__rx.set(((rx_frame >> rx_bit) & 1) != 0);
      if (++rx_t >= bit_cycles) { rx_t = 0; if (++rx_bit == 10) { rx_busy = false; top.p_uart__rx.set(true); } }
    }
    top.p_clk.set(false);
    top.step();
    // commit trace (registered W-stage outputs)
    unsigned kind = top.p_tr__kind.get<unsigned>();
    if (kind) {
      uint64_t pc = top.p_tr__pc.get<uint64_t>();
      if (kind == 1) {
        commits++;
        if (log) {
          unsigned rd = top.p_tr__rd.get<unsigned>();
          fprintf(log, "core   0: %u 0x%016llx (0x%08x)", top.p_tr__priv.get<unsigned>(),
                  (unsigned long long)pc, top.p_tr__insn.get<uint32_t>());
          if (rd) fprintf(log, " x%-2u 0x%016llx", rd, (unsigned long long)top.p_tr__data.get<uint64_t>());
          if (top.p_tr__store.get<bool>())
            fprintf(log, " st 0x%08llx 0x%016llx", (unsigned long long)top.p_tr__addr.get<uint64_t>(),
                    (unsigned long long)top.p_tr__wdata.get<uint64_t>());
          if (top.p_tr__nondet.get<bool>()) fprintf(log, " nd");
          fputc('\n', log);
        }
        if (tohost && top.p_tr__store.get<bool>() && top.p_tr__addr.get<uint64_t>() == tohost) {
          uint64_t v = top.p_tr__wdata.get<uint64_t>();
          if (v & 1) { exit_code = (int)(v >> 1); why = "tohost"; cyc++; break; }
        }
      } else if (log) {
        fprintf(log, "core   0: %s %u epc 0x%016llx tval 0x%016llx\n",
                kind == 2 ? "exception" : "interrupt", top.p_tr__cause.get<unsigned>(),
                (unsigned long long)pc, (unsigned long long)top.p_tr__tval.get<uint64_t>());
      }
    }
    if (top.p_fin__done.get<bool>()) {
      uint32_t v = top.p_fin__value.get<uint32_t>();
      exit_code = ((v & 0xffff) == 0x3333) ? (int)((v >> 16) ? (v >> 16) : 1) : 0;
      why = "finisher";
      break;
    }
    // RAM slave: act on the first cycle of a request, acknowledge next cycle
    bool cyc_ = top.p_ram__cyc.get<bool>();
    bool req = cyc_ && !ack;
    if (req) {
      uint64_t adr = top.p_ram__adr.get<uint64_t>();   // offset from RAM_BASE
      uint64_t idx = (adr >> 3) % ram.size();
      if (top.p_ram__we.get<bool>()) {
        uint64_t w = top.p_ram__dat__w.get<uint64_t>();
        unsigned sel = top.p_ram__sel.get<unsigned>();
        uint64_t mask = 0;
        for (int b = 0; b < 8; b++) if (sel & (1u << b)) mask |= 0xffull << (8 * b);
        ram[idx] = (ram[idx] & ~mask) | (w & mask);
      }
      rdata = ram[idx];
    }
    ack = req;
    // UART TX decoder: sample in the middle of each bit
    bool tx = top.p_uart__tx.get<bool>();
    switch (tx_state) {
      case 0: if (!tx) { tx_state = 1; tx_t = 0; } break;
      case 1:
        if (++tx_t == bit_cycles / 2) { if (tx) tx_state = 0; else { tx_state = 2; tx_t = 0; tx_bit = 0; tx_byte = 0; } }
        break;
      case 2:
        if (++tx_t == bit_cycles) {
          tx_t = 0;
          if (tx_bit < 8) { tx_byte |= (uint8_t)(tx ? 1 : 0) << tx_bit; tx_bit++; }
          else {
            tx_state = 0;
            out += (char)tx_byte;
            if (uout) { fputc(tx_byte, uout); fflush(uout); }
          }
        }
        break;
    }
    // scripted input: "command[<TAB>text]" lines. The next command is typed after a
    // new prompt, or, if the previous line named a text, once that text has appeared
    // (background demos print after the prompt returns).
    if (!script.empty() && !rx_busy && rx_q.empty()) {
      bool ready;
      if (!waiting.empty()) {
        ready = out.find(waiting, sent_at) != string::npos;
      } else {
        ready = out.size() >= prompt.size() && out.size() > prompt_seen &&
                out.compare(out.size() - prompt.size(), prompt.size(), prompt) == 0;
      }
      if (ready) {
        prompt_seen = out.size();
        sent_at = out.size();
        string line = script.front();
        script.pop_front();
        size_t tab = line.find('\t');
        waiting.clear();
        if (tab != string::npos) {
          waiting = line.substr(tab + 1);
          while (!waiting.empty() && (waiting.back() == '\n' || waiting.back() == '\r')) waiting.pop_back();
          line = line.substr(0, tab) + "\n";
        }
        for (char c : line) rx_q.push_back((uint8_t)c);
      }
    }
    top.p_clk.set(true);
    top.step();
  }
  if (log) fclose(log);
  if (uout) fclose(uout);
  FILE *pf = perf_path ? fopen(perf_path, "w") : stdout;
  fprintf(pf, "{\"stop\": \"%s\", \"exit_code\": %d, \"sim_cycles\": %llu, \"commits\": %llu",
          why, exit_code, (unsigned long long)cyc, (unsigned long long)commits);
#define P(s, n) fprintf(pf, ", \"" s "\": %llu", (unsigned long long)top.p_perf__##n.get<uint64_t>());
  P("cycles", cycles) P("instret", instret) P("ic_access", ic__access) P("ic_miss", ic__miss)
  P("dc_access", dc__access) P("dc_miss", dc__miss) P("branches", branches)
  P("mispredicts", mispredicts) P("itlb_miss", itlb__miss) P("dtlb_miss", dtlb__miss)
  P("walks", walks) P("stall_load_use", stall__load__use) P("stall_mem", stall__mem)
  P("traps", traps)
  fprintf(pf, "}\n");
  if (pf != stdout) fclose(pf);
  return 0;
}
