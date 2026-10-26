"""各 skill は ``.claude/skills/<name>/`` に実体を置き、symlink で他所を参照しない."""

from pcbasm.paths import PROJECT_ROOT

SKILLS = PROJECT_ROOT / ".claude" / "skills"


class TestSkills:
    def test_every_skill_is_a_real_directory_with_skill_md(self):
        skills = sorted(SKILLS.iterdir())

        assert skills
        assert [skill.name for skill in skills if skill.is_symlink()] == []
        assert [s.name for s in skills if not (s / "SKILL.md").is_file()] == []
