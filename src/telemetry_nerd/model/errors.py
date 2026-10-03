class NotFound(Exception):
    """A workspace object or dataset id does not exist."""


class WrongWorkspace(ValueError):
    """An update by id reached an object that lives in another workspace (ids are global)."""

    def __init__(self, obj_id: str, workspace: str, title: str | None = None) -> None:
        named = f"{workspace} '{title}'" if title else workspace
        super().__init__(f"{obj_id} belongs to workspace {named}; workspace_switch to it first")
        self.obj_id = obj_id
        self.workspace = workspace
        self.title = title
