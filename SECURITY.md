# Security policy

Sosopo stores publishing credentials and should be deployed only behind HTTPS with a persistent encryption key. Keep `.env`, `data/`, `backups/`, and mounted secret files private.

## Reporting a vulnerability

Do not report credential exposure, authentication bypasses, data leaks, or remote-code-execution risks in a public issue. Contact the repository maintainer privately through the repository's security advisory/reporting feature. Include affected version, impact, a minimal reproduction, and any mitigation you have identified. Rotate any credential that may have been exposed.

There is no guaranteed support window until release tags are established. Before upgrading a dependency or image digest, test the build, automated suite, backup verification, and provider sandbox flow.
