# Security

Keep data directories, model keys, account tokens, password databases and deployment secrets outside Git. Do not include them in issue reports.

Default listeners are loopback-only. Remote access requires an operator-managed HTTPS gateway, authentication and backups. Do not expose MCP ports or raw agent runtime services directly to the public Internet.

This project is evolving. Review its documented permissions and recovery limits before granting agents access to sensitive files or systems. A file sandbox does not imply network isolation.

For a suspected credential exposure, revoke the affected credential first. Avoid posting exploit details or private data in public issues; use GitHub private vulnerability reporting if enabled by the repository owner.
