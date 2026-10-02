from __future__ import annotations

from typing import TYPE_CHECKING

from tomlkit.container import Container


if TYPE_CHECKING:
    from collections.abc import Sequence

    from typing import Self

    from tomlkit.batch import Operation


class TOMLDocument(Container):
    """
    A TOML document.
    """

    def batch(self, operations: Sequence[Operation]) -> Self:
        from tomlkit.batch import batch as _batch

        return _batch(self, operations)

    def apply_batch(self, operations: Sequence[Operation]) -> Self:
        from tomlkit.batch import batch as _batch

        return _batch(self, operations)
