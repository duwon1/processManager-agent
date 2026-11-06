import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm_agent.platforms.linux import security

INSTALL_SH_PATH = (
    Path(__file__).resolve().parents[2]
    / "processManager" / "backend" / "src" / "main" / "resources" / "static" / "agent" / "install.sh"
)


class LinuxSudoersTests(unittest.TestCase):
    def build(self, **overrides):
        kwargs = dict(
            agent_user="ubuntu",
            service_name="processmanager-agent-dev",
            terminal_user="processmanager-terminal",
            terminal_shell="/bin/bash",
            systemctl_bin="/usr/bin/systemctl",
            rm_bin="/bin/rm",
            dmidecode_bin="/usr/sbin/dmidecode",
        )
        kwargs.update(overrides)
        return security.build_agent_sudoers(**kwargs)

    def test_never_grants_all_commands(self):
        content = self.build()
        self.assertNotIn("NOPASSWD: ALL", content)
        self.assertNotIn("(ALL)", content)

    def test_contains_only_commands_the_agent_uses(self):
        content = self.build()
        root_line = content.splitlines()[0]
        expected = (
            "ubuntu ALL=(root) NOPASSWD: "
            "/usr/bin/systemctl restart processmanager-agent-dev, "
            "/usr/bin/systemctl stop processmanager-agent-dev, "
            "/usr/bin/systemctl disable processmanager-agent-dev, "
            "/usr/bin/systemctl daemon-reload, "
            "/bin/rm -f /etc/systemd/system/processmanager-agent-dev.service, "
            "/usr/local/lib/processmanager/pm-kill, "
            "/usr/local/lib/processmanager/pm-service, "
            "/usr/sbin/dmidecode -t 17, "
            "/usr/sbin/dmidecode -t memory"
        )
        self.assertEqual(root_line, expected)
        self.assertIn("ubuntu ALL=(processmanager-terminal) NOPASSWD: /bin/bash --login", content)

    def test_no_abusable_wildcards(self):
        content = self.build()
        self.assertNotIn("*", content)
        self.assertNotIn("kill -9", content)
        self.assertNotIn("systemctl start", content)

    def test_matches_install_sh_sudoers(self):
        """install.sh의 sudoers printf와 build_agent_sudoers 결과가 같은지 확인합니다."""
        install_sh = INSTALL_SH_PATH
        if not install_sh.is_file():
            self.skipTest("install.sh가 없는 환경입니다.")
        text = install_sh.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if "printf '%s ALL=(root) NOPASSWD:" in line)
        fmt = re.search(r"printf '([^']*)'", lines[start]).group(1)
        # printf 다음 줄부터 줄 끝 역슬래시(\)로 이어진 인자 목록을 모읍니다.
        arg_text = ""
        for line in lines[start + 1:]:
            arg_text += line
            if not line.rstrip().endswith("\\"):
                break
        args = re.findall(r'"\$([A-Z_]+)"', arg_text)
        values = {
            "AGENT_USER": "ubuntu",
            "SYSTEMCTL_PATH": "/usr/bin/systemctl",
            "SERVICE_NAME": "processmanager-agent-dev",
            "RM_PATH": "/bin/rm",
            "PM_KILL_PATH": "/usr/local/lib/processmanager/pm-kill",
            "PM_SERVICE_PATH": "/usr/local/lib/processmanager/pm-service",
            "DMIDECODE_PATH": "/usr/sbin/dmidecode",
        }
        rendered = fmt.replace("\\n", "")
        for name in args:
            rendered = rendered.replace("%s", values[name], 1)
        self.assertNotIn("%s", rendered)
        self.assertEqual(rendered, self.build().splitlines()[0])
        self.assertIn("limited-sudoers-v3", text)
        self.assertIn("visudo -cf", text)

    def test_does_not_grant_root_or_relative_terminal_shell(self):
        self.assertNotIn("--login", self.build(terminal_user="root"))
        self.assertNotIn("--login", self.build(terminal_user=""))
        self.assertNotIn("--login", self.build(terminal_shell="bash"))

    def test_rejects_unsafe_names(self):
        with self.assertRaises(ValueError):
            self.build(service_name="evil, ALL")
        with self.assertRaises(ValueError):
            self.build(agent_user="u ALL=(ALL) NOPASSWD: ALL")

    def test_marker_matches_installer(self):
        self.assertEqual(security.HARDENING_MARKER_VERSION, "limited-sudoers-v3")
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / security.HARDENING_MARKER
            marker.write_text("limited-sudoers-v3\ninstalled by install.sh\n", encoding="utf-8")
            self.assertTrue(security.marker_is_current(marker))
            marker.write_text("full-sudoers-v1\ninstalled\n", encoding="utf-8")
            self.assertFalse(security.marker_is_current(marker))

    def test_installer_marker_skips_runtime_sudoers_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / security.HARDENING_MARKER).write_text(
                "limited-sudoers-v3\ninstalled by install.sh\n", encoding="utf-8"
            )
            with mock.patch.object(security, "run_sudo") as run_sudo:
                success, _ = asyncio.run(security.ensure_agent_sudoers(tmp, "processmanager-agent"))
            self.assertTrue(success)
            run_sudo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
