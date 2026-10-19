from pathlib import Path

import pytest

from muliao.errors import InvalidInput, NotFound
from muliao.evolution.memory import MemoryFull, MemoryStore
from muliao.evolution.skills import SkillStore


@pytest.fixture
def skills(tmp_path: Path) -> SkillStore:
    return SkillStore(tmp_path / "skills", tmp_path / "agents")


def test_private_skill_shadows_public_skill_with_same_name(skills: SkillStore):
    skills.write(None, "digest", "public digest", "public body")
    skills.write("alpha", "digest", "private digest", "private body")
    skills.write(None, "other", "other public", "x")

    visible = {(s.name, s.scope) for s in skills.visible_to("alpha")}

    assert visible == {("digest", "private"), ("other", "public")}
    assert skills.resolve("alpha", "digest").body.strip() == "private body"
    assert skills.resolve("beta", "digest").body.strip() == "public body"


def test_skill_roundtrip_preserves_frontmatter(skills: SkillStore):
    skills.write("alpha", "slack-digest", "Summarise slack", "# Steps\n1. fetch")

    skill = skills.read("alpha", "slack-digest")

    assert skill.meta.description == "Summarise slack"
    assert skill.body.startswith("# Steps")


def test_skill_rejects_invalid_name_and_empty_description(skills: SkillStore):
    with pytest.raises(InvalidInput):
        skills.write(None, "../escape", "d", "b")
    with pytest.raises(InvalidInput):
        skills.write(None, "ok", "  ", "b")


def test_skill_with_mismatched_frontmatter_name_fails_loudly(skills: SkillStore, tmp_path: Path):
    d = tmp_path / "skills" / "real"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: fake\ndescription: x\n---\nbody")

    with pytest.raises(InvalidInput, match="does not match directory"):
        skills.list(None)


def test_delete_missing_skill_raises_not_found(skills: SkillStore):
    with pytest.raises(NotFound):
        skills.delete(None, "nope")


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path, max_chars=50)
    store.init("alpha")
    return store


def test_memory_append_and_replace(memory: MemoryStore):
    memory.append("alpha", "- likes tea")
    memory.append("alpha", "- lives in Paris")
    memory.replace("alpha", "Paris", "Berlin")

    assert memory.read("alpha") == "- likes tea\n- lives in Berlin\n"


def test_memory_replace_requires_exactly_one_occurrence(memory: MemoryStore):
    memory.append("alpha", "a a")
    with pytest.raises(InvalidInput, match="found 2"):
        memory.replace("alpha", "a", "b")


def test_memory_over_limit_is_rejected_and_unchanged(memory: MemoryStore):
    memory.append("alpha", "short")
    with pytest.raises(MemoryFull):
        memory.append("alpha", "x" * 60)
    assert memory.read("alpha") == "short\n"


def test_memory_of_unknown_agent_is_not_found(memory: MemoryStore):
    with pytest.raises(NotFound):
        memory.read("ghost")


async def test_vault_renders_references_and_fails_on_missing_key(container):
    vault = container.vault
    await vault.set("TOKEN", "s3cret", "api token")

    assert await vault.render("Bearer ${vault:TOKEN}") == "Bearer s3cret"
    assert [s.key for s in await vault.list()] == ["TOKEN"]
    with pytest.raises(NotFound):
        await vault.render("${vault:MISSING}")
    with pytest.raises(InvalidInput):
        await vault.set("bad-key", "v")
