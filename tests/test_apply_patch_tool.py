from pathlib import Path

from miniclaw.agent.tools.apply_patch import ApplyPatchTool


async def test_apply_patch_add_update_delete(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (tmp_path / "old.txt").write_text("legacy\n", encoding="utf-8")

    tool = ApplyPatchTool(allowed_dir=tmp_path)
    patch = """*** Begin Patch
*** Add File: new.txt
+hello
+world
*** Update File: keep.txt
@@
 alpha
-beta
+beta2
*** End of File
*** Delete File: old.txt
*** End Patch"""

    result = await tool.execute(patch=patch)

    assert "Added new.txt" in result
    assert "Updated keep.txt" in result
    assert "Deleted old.txt" in result
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\nworld"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "alpha\nbeta2\n"
    assert not (tmp_path / "old.txt").exists()


async def test_apply_patch_update_move_with_and_without_hunks(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "rename_only.txt").write_text("stable\n", encoding="utf-8")

    tool = ApplyPatchTool(allowed_dir=tmp_path)
    patch = """*** Begin Patch
*** Update File: source.txt
*** Move to: moved/target.txt
@@
 one
-two
+three
*** Update File: rename_only.txt
*** Move to: moved/rename_only.txt
*** End Patch"""

    result = await tool.execute(patch=patch)

    assert "Updated source.txt -> moved/target.txt" in result
    assert "Updated rename_only.txt -> moved/rename_only.txt" in result
    assert not (tmp_path / "source.txt").exists()
    assert not (tmp_path / "rename_only.txt").exists()
    assert (tmp_path / "moved" / "target.txt").read_text(encoding="utf-8") == "one\nthree\n"
    assert (tmp_path / "moved" / "rename_only.txt").read_text(encoding="utf-8") == "stable\n"


async def test_apply_patch_rejects_invalid_markers(tmp_path: Path) -> None:
    tool = ApplyPatchTool(allowed_dir=tmp_path)
    result = await tool.execute(patch="not a patch")
    assert "must start with" in result


async def test_apply_patch_rejects_ambiguous_hunk(tmp_path: Path) -> None:
    (tmp_path / "dup.txt").write_text("x\ny\nx\ny\n", encoding="utf-8")
    tool = ApplyPatchTool(allowed_dir=tmp_path)
    patch = """*** Begin Patch
*** Update File: dup.txt
@@
 x
-y
+z
*** End Patch"""

    result = await tool.execute(patch=patch)
    assert "matched multiple regions" in result


async def test_apply_patch_rejects_missing_hunk_target(tmp_path: Path) -> None:
    (tmp_path / "single.txt").write_text("a\nb\n", encoding="utf-8")
    tool = ApplyPatchTool(allowed_dir=tmp_path)
    patch = """*** Begin Patch
*** Update File: single.txt
@@
 q
-z
+x
*** End Patch"""

    result = await tool.execute(patch=patch)
    assert "did not match target file" in result


async def test_apply_patch_respects_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)

    outside = (tmp_path / "outside.txt").resolve()
    tool = ApplyPatchTool(allowed_dir=allowed)
    patch = f"""*** Begin Patch
*** Add File: {outside}
+oops
*** End Patch"""

    result = await tool.execute(patch=patch)
    assert "outside allowed directory" in result


async def test_apply_patch_rejects_prefix_sibling_outside_allowed_dir(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir(parents=True, exist_ok=True)

    sibling = (tmp_path / "allowed_evil" / "outside.txt").resolve()
    tool = ApplyPatchTool(allowed_dir=allowed)
    patch = f"""*** Begin Patch
*** Add File: {sibling}
+oops
*** End Patch"""

    result = await tool.execute(patch=patch)
    assert "outside allowed directory" in result
