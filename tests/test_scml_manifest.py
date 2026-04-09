from __future__ import annotations

import unittest
from pathlib import Path

from tools.build_scml_manifest import build_manifest


def find_syscall(manifest: dict[str, object], category: str, name: str) -> dict[str, object]:
    categories = manifest["categories"]
    return categories[category]["syscalls"][name]


class SCMLManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_dir = Path("third_party/asterinas")
        cls.source_root = cls.repo_dir / "book/src/kernel/linux-compatibility/syscall-flag-coverage"
        cls.manifest = build_manifest(
            target="asterinas",
            repo_dir=cls.repo_dir,
            source_root=cls.source_root,
        )

    def test_manifest_stats_are_self_consistent(self) -> None:
        manifest = self.manifest
        categories = manifest["categories"]
        total_syscalls = sum(category["syscall_count"] for category in categories.values())
        total_files = sum(len(category["source_files"]) for category in categories.values())
        self.assertEqual(manifest["stats"]["category_count"], len(categories))
        self.assertEqual(manifest["stats"]["total_syscalls"], total_syscalls)
        self.assertEqual(manifest["stats"]["scml_file_count"], total_files)
        self.assertGreaterEqual(total_syscalls, 200)

    def test_openat_metadata_preserves_partial_support_notes(self) -> None:
        syscall = find_syscall(
            self.manifest,
            "file-and-directory-operations",
            "openat",
        )
        self.assertEqual(syscall["support_tier"], "partial")
        self.assertIn(
            "third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage/file-and-directory-operations/open_and_openat.scml",
            syscall["source_scml_files"],
        )
        self.assertIn("O_NOCTTY", syscall["ignored"]["flags"])
        self.assertIn("O_TMPFILE", syscall["unsupported"]["flags"])
        self.assertIn("O_PATH", syscall["partial"]["flags"])
        self.assertIn("O_NOCTTY", syscall["ignored_flags"])
        self.assertIn("O_TMPFILE", syscall["unsupported_flags"])
        self.assertIn("O_PATH", syscall["partial_flags"])
        self.assertIsNone(syscall["defer_reason"])

    def test_fully_covered_close_is_marked_full(self) -> None:
        syscall = find_syscall(
            self.manifest,
            "file-and-directory-operations",
            "close",
        )
        self.assertEqual(syscall["support_tier"], "full")
        self.assertTrue(
            all(path.endswith("fully_covered.scml") for path in syscall["source_scml_files"])
        )
        self.assertEqual(syscall["ignored"], {})
        self.assertEqual(syscall["partial"], {})
        self.assertEqual(syscall["unsupported"], {})
        self.assertEqual(syscall["ignored_flags"], [])
        self.assertEqual(syscall["partial_flags"], [])
        self.assertEqual(syscall["unsupported_flags"], [])

    def test_socket_metadata_stays_in_networking_category(self) -> None:
        syscall = find_syscall(
            self.manifest,
            "networking-and-sockets",
            "socket",
        )
        self.assertEqual(syscall["category"], "networking-and-sockets")
        self.assertEqual(syscall["support_tier"], "partial")
        self.assertIn(
            "third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage/networking-and-sockets/socket.scml",
            syscall["source_scml_files"],
        )
        self.assertIn("socket", syscall["readme_headings"][0].lower())

    def test_alias_fields_follow_bucket_content(self) -> None:
        syscall = find_syscall(
            self.manifest,
            "file-and-directory-operations",
            "renameat2",
        )
        self.assertEqual(syscall["unsupported_flags"], syscall["unsupported"]["flags"])
        self.assertEqual(syscall["ignored_flags"], [])
        self.assertEqual(syscall["partial_flags"], [])

    def test_manifest_alias_values_strip_readme_prose(self) -> None:
        getrandom = find_syscall(
            self.manifest,
            "system-information-and-misc",
            "getrandom",
        )
        arch_prctl = find_syscall(
            self.manifest,
            "system-information-and-misc",
            "arch_prctl",
        )
        self.assertEqual(getrandom["ignored_flags"], ["GRND_NONBLOCK"])
        self.assertIn("ARCH_GET_CPUID", arch_prctl["unsupported_codes"])
        self.assertIn("ARCH_SET_CPUID", arch_prctl["unsupported_codes"])


if __name__ == "__main__":
    unittest.main()
