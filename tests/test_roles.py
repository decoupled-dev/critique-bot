"""roles: names, spellings, skills, and the prompt sections behind each role."""

from __future__ import annotations

import unittest
from pathlib import Path

from critique_bot import roles
from critique_bot.agent import load_prompt_sections, load_role_instructions, seed_message
from critique_bot.agent_shell import Shell


class RoleNamesTests(unittest.TestCase):
    def test_the_seven_roles_in_order(self) -> None:
        self.assertEqual(
            roles.ROLE_IDS,
            (
                "code-reviewer",
                "aosp-expert",
                "expert-developer",
                "ai-expert",
                "test-engineer",
                "android-app-engineer",
                "generic",
            ),
        )
        self.assertEqual([role.label for role in roles.ROLES][:2], ["Code Reviewer", "AOSP Expert"])

    def test_spellings_from_the_flag_the_setting_and_the_screen(self) -> None:
        for raw in ("AOSPExpert", "aosp-expert", "aosp_expert", "AOSP Expert", "aosp", " Aosp Expert "):
            self.assertEqual(roles.normalize_role(raw), "aosp-expert", raw)
        self.assertEqual(roles.normalize_role("CodeReviewer"), "code-reviewer")
        self.assertEqual(roles.normalize_role("ExpertDeveloper"), "expert-developer")
        self.assertEqual(roles.normalize_role("AIExpert"), "ai-expert")
        self.assertEqual(roles.normalize_role("TestEngineer"), "test-engineer")
        self.assertEqual(roles.normalize_role("AndroidAppEngineer"), "android-app-engineer")
        self.assertEqual(roles.normalize_role("Generic"), "generic")
        self.assertEqual(roles.normalize_role("default"), "generic")
        self.assertIsNone(roles.normalize_role("wizard"))
        self.assertIsNone(roles.normalize_role(""))
        self.assertIsNone(roles.normalize_role(None))

    def test_unknown_role_falls_back_to_generic(self) -> None:
        self.assertEqual(roles.role_by_id("wizard").id, "generic")
        self.assertEqual(roles.role_label(None), "Generic")
        self.assertEqual(roles.role_label("android-app-engineer"), "Android App Engineer")

    def test_role_skills_are_built_in_skills(self) -> None:
        skills_dir = Path(__file__).resolve().parents[1] / "src" / "critique_bot" / "skills"
        self.assertEqual(roles.role_skills("aosp-expert"), ["aosp"])
        self.assertEqual(roles.role_skills("android-app-engineer"), ["android"])
        self.assertEqual(roles.role_skills("generic"), [])
        self.assertEqual(roles.role_skills("code-reviewer"), [])
        for role in roles.ROLES:
            for name in role.skills:
                self.assertTrue((skills_dir / name / "SKILL.md").is_file(), f"{role.id} pins unknown skill {name}")


class RolePromptTests(unittest.TestCase):
    def test_every_role_has_a_prompt_section(self) -> None:
        sections = load_prompt_sections()
        for role in roles.ROLES:
            key = roles.section_key(role.id)
            self.assertIn(key, sections, f"prompts/agent.txt needs a <<<{key}>>> section")
            text = load_role_instructions(role.id)
            if role.id == roles.GENERIC:
                self.assertEqual(text, "")
            else:
                self.assertTrue(text.startswith(f"ROLE: {role.label}."), (role.id, text[:60]))
                self.assertNotIn("<<<", text)

    def test_role_text_follows_the_instructions_in_the_seed(self) -> None:
        bash = Shell("bash", "bash", "bash -lc")
        role = load_role_instructions("code-reviewer")
        text = seed_message(Path("."), "INSTRUCTIONS", shell=bash, platform_name="linux", role=role)
        self.assertLess(text.index("INSTRUCTIONS"), text.index("ROLE: Code Reviewer."))
        self.assertLess(text.index("ROLE: Code Reviewer."), text.index("ENVIRONMENT"))
        self.assertIn("Reply with exactly READY.", text)
        plain = seed_message(Path("."), "INSTRUCTIONS", shell=bash, platform_name="linux", role="")
        self.assertNotIn("ROLE:", plain)
        self.assertEqual(seed_message(Path("."), "INSTRUCTIONS", shell=bash, platform_name="linux"), plain)


class ResolveRoleTests(unittest.TestCase):
    def test_flag_then_setting_then_generic(self) -> None:
        from critique_bot.agent import _resolve_role

        self.assertEqual(_resolve_role("TestEngineer", {"role": "aosp-expert"}), "test-engineer")
        self.assertEqual(_resolve_role(None, {"role": "AOSP Expert"}), "aosp-expert")
        self.assertEqual(_resolve_role(None, {}), "generic")
        self.assertEqual(_resolve_role(None, {"role": "wizard"}), "generic")
        self.assertEqual(_resolve_role("", {"role": ""}), "generic")


if __name__ == "__main__":
    unittest.main()
