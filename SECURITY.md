# Security policy

orbit-rv is a teaching/portfolio CPU design, not a security boundary: there is
no PMP, no side-channel hardening and no formal verification of the privilege
checks. Do not rely on it to isolate untrusted code.

If you find a bug that breaks privilege isolation as specified by the RISC-V
privileged architecture (for example U-mode reaching S-mode pages, or a
missed permission check in the MMU), please open a GitHub issue with a
minimal program that shows it, or use GitHub's private vulnerability
reporting for this repository if you prefer not to disclose it publicly.
