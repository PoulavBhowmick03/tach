import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from itertools import chain
from typing import Optional, Generator, Callable

from prompt_toolkit import ANSI
from prompt_toolkit.data_structures import Point
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.tree import Tree
from rich.text import Text
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, Window, ScrollablePane
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from tach import errors
from tach import filesystem as fs
from tach.constants import PACKAGE_FILE_NAME


@dataclass
class SelectedPackage:
    full_path: str

    @property
    def tags(self) -> list[str]:
        # In the future, might make this an attribute of the dataclass
        # and set it interactively
        return [fs.file_to_module_path(self.full_path)]


@dataclass
class FileNode:
    full_path: str
    is_dir: bool
    expanded: bool = False
    is_package: bool = False
    parent: Optional["FileNode"] = None
    children: list["FileNode"] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return len(self.children) == 0

    @property
    def visible_children(self) -> list["FileNode"]:
        if not self.expanded:
            return []
        return self.children

    @classmethod
    def build_from_path(cls, path: str) -> "FileNode":
        is_dir = os.path.isdir(path)
        is_package = os.path.isfile(os.path.join(path, f"{PACKAGE_FILE_NAME}.yml"))
        return cls(full_path=path, is_dir=is_dir, is_package=is_package)

    @property
    def parent_sorted_children(self) -> Optional[list["FileNode"]]:
        if not self.parent:
            return None
        return sorted(self.parent.visible_children, key=lambda node: node.full_path)

    @property
    def prev_sibling(self) -> Optional["FileNode"]:
        parent_sorted_children = self.parent_sorted_children
        if not parent_sorted_children:
            return None

        try:
            my_index = parent_sorted_children.index(self)
        except ValueError:
            raise errors.TachError("Error occurred in interactive file tree navigation")

        if my_index == 0:
            return None
        return parent_sorted_children[my_index - 1]

    @property
    def next_sibling(self) -> Optional["FileNode"]:
        parent_sorted_children = self.parent_sorted_children
        if not parent_sorted_children:
            return None

        try:
            my_index = parent_sorted_children.index(self)
        except ValueError:
            raise errors.TachError("Error occurred in interactive file tree navigation")

        if my_index == len(parent_sorted_children) - 1:
            return None
        return parent_sorted_children[my_index + 1]


@dataclass
class FileTree:
    root: FileNode
    nodes: dict[str, FileNode] = field(default_factory=dict)

    @classmethod
    def build_from_path(cls, path: str, depth: int = 1) -> "FileTree":
        root = FileNode.build_from_path(fs.canonical(path))
        root.expanded = True
        tree = cls(root)
        tree.nodes[path] = root
        tree._build_subtree(root, depth=depth)
        return tree

    def _build_subtree(self, root: FileNode, depth: int = 1):
        if root.is_dir:
            try:
                for entry in os.listdir(root.full_path):
                    if entry.startswith("."):
                        # Ignore hidden files and directories
                        continue
                    entry_path = os.path.join(root.full_path, entry)
                    if not os.path.isdir(entry_path):
                        # Only interested in directories for now
                        continue
                    child_node = FileNode.build_from_path(entry_path)
                    if depth > 0:
                        child_node.expanded = True
                    child_node.parent = root
                    root.children.append(child_node)
                    self.nodes[entry_path] = child_node
                    if child_node.is_dir:
                        self._build_subtree(child_node, max(depth - 1, 0))
            except PermissionError:
                # This is expected to occur during listdir when the directory cannot be accessed
                # We simply bail if that happens, meaning it won't show up in the interactive viewer
                return

    def expand_path(self, path: str):
        if path not in self.nodes:
            raise errors.TachError(f"Directory {path} not found in tree.")
        node = self.nodes[path]
        if not node.is_dir:
            raise errors.TachError(
                f"{path} does not seem to be a directory and cannot be expanded."
            )

        self._build_subtree(node, depth=1)

    def __iter__(self):
        return file_tree_iterator(self)

    def visible(self):
        return file_tree_iterator(self, visible_only=True)


def file_tree_iterator(
    tree: FileTree, visible_only: bool = False
) -> Generator[FileNode, None, None]:
    # DFS traversal for printing
    stack = deque([tree.root])

    while stack:
        node = stack.popleft()
        yield node
        if visible_only:
            stack.extendleft(
                sorted(node.visible_children, key=lambda n: n.full_path, reverse=True)
            )
        else:
            stack.extendleft(
                sorted(node.children, key=lambda n: n.full_path, reverse=True)
            )


class ExitCode(Enum):
    QUIT_NOSAVE = 1
    QUIT_SAVE = 2


class InteractivePackageTree:
    def __init__(self, path: str, depth: int = 1):
        # By default, don't save if we exit for any reason
        self.exit_code: ExitCode = ExitCode.QUIT_NOSAVE
        self.file_tree = FileTree.build_from_path(path=path, depth=depth)
        self.selected_node = self.file_tree.root
        # x location doesn't matter, only need to track hidden cursor for auto-scroll behavior
        # y location starts at 1 because the FileTree is rendered with a labeled header above the first branch
        self.cursor_point = Point(x=0, y=1)
        self.console = Console()
        self.tree_control = FormattedTextControl(
            text=ANSI(self._render_tree()),
            focusable=True,
            show_cursor=False,
            get_cursor_position=self.get_cursor_position_fn(),
        )
        self.footer_control = self._build_footer()
        self.layout = Layout(
            HSplit(
                [
                    Frame(ScrollablePane(Window(self.tree_control))),
                    Window(self.footer_control),
                ]
            )
        )
        self.key_bindings = KeyBindings()
        self._register_keybindings()
        self.styles = self._build_styles()
        self.app = Application(
            layout=self.layout,
            key_bindings=self.key_bindings,
            full_screen=True,
            style=self.styles,
        )

    def get_cursor_position_fn(self) -> Callable[[], Point]:
        def get_cursor_position() -> Point:
            return self.cursor_point

        return get_cursor_position

    def move_cursor_up(self):
        self.cursor_point = Point(x=self.cursor_point.x, y=self.cursor_point.y - 1)

    def move_cursor_down(self):
        self.cursor_point = Point(x=self.cursor_point.x, y=self.cursor_point.y + 1)

    @staticmethod
    def _build_styles() -> Style:
        return Style.from_dict(
            {
                "footer-key": "bold cyan",
            }
        )

    KEY_BINDING_LEGEND = [
        ("Ctrl + c", "Exit without saving"),
        ("Ctrl + s", "Save packages"),
        ("Enter", "Mark/unmark package"),
        ("Up/Down", "Navigate"),
        ("Right", "Expand"),
        ("Left", "Collapse"),
    ]

    @staticmethod
    def _key_binding_text(binding: str, description: str) -> list[tuple[str, str]]:
        return [("class:footer-key", binding), ("", f": {description}  ")]

    @classmethod
    def _build_footer(cls) -> FormattedTextControl:
        footer_text = list(
            chain(
                *(
                    cls._key_binding_text(legend[0], legend[1])
                    for legend in cls.KEY_BINDING_LEGEND
                )
            )
        )
        return FormattedTextControl(text=footer_text)

    def _register_keybindings(self):
        if self.key_bindings.bindings:
            return

        @self.key_bindings.add("c-c")
        def _(event):
            self.exit_code = ExitCode.QUIT_NOSAVE
            self.app.exit()

        @self.key_bindings.add("c-s")
        def _(event):
            self.exit_code = ExitCode.QUIT_SAVE
            self.app.exit()

        @self.key_bindings.add("up")
        def up(event):
            prev_sibling = self.selected_node.prev_sibling
            # If previous sibling exists, want to bubble down to last child of this sibling
            if prev_sibling:
                curr_node = prev_sibling
                while curr_node.visible_children:
                    curr_node = sorted(
                        curr_node.visible_children, key=lambda node: node.full_path
                    )[-1]
                self.selected_node = curr_node
                self.move_cursor_up()
                self._update_display()
            # If no previous sibling, go to parent
            elif self.selected_node.parent:
                self.selected_node = self.selected_node.parent
                self.move_cursor_up()
                self._update_display()

        @self.key_bindings.add("down")
        def down(event):
            # If we have children, should go to first child alphabetically
            if self.selected_node.visible_children:
                self.selected_node = sorted(
                    self.selected_node.visible_children, key=lambda node: node.full_path
                )[0]
                self.move_cursor_down()
                self._update_display()
                return
            # If we have no children and no parent, nothing to do
            elif not self.selected_node.parent:
                return

            # Here we need to bubble up to find the next node
            curr_node = self.selected_node
            next_sibling = self.selected_node.next_sibling
            while next_sibling is None:
                if not curr_node.parent:
                    break
                curr_node = curr_node.parent
                next_sibling = curr_node.next_sibling

            if not next_sibling:
                # We are the last child all the way up to root
                return

            self.selected_node = next_sibling
            self.move_cursor_down()
            self._update_display()

        @self.key_bindings.add("right")
        def right(event):
            self.selected_node.expanded = True
            self._update_display()

        @self.key_bindings.add("left")
        def left(event):
            self.selected_node.expanded = False
            self._update_display()

        @self.key_bindings.add("enter")
        def enter(event):
            self.selected_node.is_package = not self.selected_node.is_package
            self._update_display()

    def _render_node(self, node: FileNode) -> Text:
        text_parts = []
        if node == self.selected_node:
            text_parts.append(("-> ", "bold cyan"))

        basename = os.path.basename(node.full_path)
        if node.is_package:
            text_parts.append((f"[Package] {basename}", "bold yellow"))
        elif node == self.selected_node:
            text_parts.append((basename, "bold"))
        else:
            text_parts.append(basename)

        if not node.empty and node.expanded:
            text_parts.append((" ∨", "cyan"))
        elif not node.empty:
            text_parts.append((" >", "cyan"))
        return Text.assemble(*text_parts)

    def _render_tree(self):
        tree_root = Tree("Packages")
        # Mapping FileNode paths to rich.Tree branches
        # so that we can iterate over the FileTree and use the
        # parent pointers to find the parent rich.Tree branches
        tree_mapping: dict[str, Tree] = {}

        for node in self.file_tree.visible():
            if node.parent is None:
                # If no parent on FileNode, add to rich.Tree root
                tree_node = tree_root.add(self._render_node(node))
            else:
                if node.parent.full_path not in tree_mapping:
                    raise errors.TachError("Failed to render package tree.")
                # Find parent rich.Tree branch,
                # attach this FileNode to the parent's branch
                parent_tree_node = tree_mapping[node.parent.full_path]
                tree_node = parent_tree_node.add(self._render_node(node))

            # Add this new FileNode to the mapping
            tree_mapping[node.full_path] = tree_node

        with self.console.capture() as capture:
            self.console.print(tree_root)
        return capture.get()

    def _update_display(self):
        self.tree_control.text = ANSI(self._render_tree())

    def run(self) -> Optional[list[SelectedPackage]]:
        self.app.run()
        if self.exit_code == ExitCode.QUIT_SAVE:
            return [
                SelectedPackage(full_path=node.full_path)
                for node in self.file_tree
                if node.is_package
            ]


def get_selected_packages_interactive(
    path: str, depth: int = 1
) -> Optional[list[SelectedPackage]]:
    ipt = InteractivePackageTree(path=path, depth=depth)
    return ipt.run()
