"""Overlay for third-party chex.dataclass decorator.

See https://github.com/deepmind/chex#dataclass-dataclasspy. Typing-wise, the
differences between @dataclasses.dataclass and @chex.dataclass are:
* The latter has a mappable_dataclass parameter, defaulting to True, which makes
  the dataclass inherit from Mapping.
* Chex dataclasses have replace, from_tuple, and to_tuple methods.
"""

from pytype import overlay
from pytype import overlay_utils
from pytype.abstract import abstract
from pytype.overlays import classgen
from pytype.overlays import dataclass_overlay
from pytype.pytd import pytd


class ChexOverlay(overlay.Overlay):

  def __init__(self, ctx):
    member_map = {
        "dataclass": Dataclass.make,
    }
    ast = ctx.loader.import_name("chex")
    super().__init__(ctx, "chex", member_map, ast)


class Dataclass(dataclass_overlay.Dataclass):
  """Implements the @dataclass decorator."""

  _DEFAULT_ARGS = {**dataclass_overlay.Dataclass._DEFAULT_ARGS,
                   "mappable_dataclass": True}

  @classmethod
  def make(cls, ctx):
    return super().make(ctx, "chex")

  def _add_replace_method(self, node, cls):
    cls.members["replace"] = classgen.make_replace_method(
        self.ctx, node, cls, kwargs_name="changes")

  def _add_from_tuple_method(self, node, cls):
    # from_tuple is discouraged anyway, so we provide only bare-bones types.
    cls.members["from_tuple"] = overlay_utils.make_method(
        ctx=self.ctx,
        node=node,
        name="from_tuple",
        params=[overlay_utils.Param("args")],
        return_type=cls,
        kind=pytd.MethodKind.STATICMETHOD,
    )

  def _add_to_tuple_method(self, node, cls):
    # to_tuple is discouraged anyway, so we provide only bare-bones types.
    cls.members["to_tuple"] = overlay_utils.make_method(
        ctx=self.ctx,
        node=node,
        name="to_tuple",
        return_type=self.ctx.convert.tuple_type,
    )

  def decorate(self, node, cls):
    super().decorate(node, cls)
    if not isinstance(cls, abstract.InterpreterClass):
      return
    self._add_replace_method(node, cls)
    self._add_from_tuple_method(node, cls)
    self._add_to_tuple_method(node, cls)
    if not self.args[cls]["mappable_dataclass"]:
      return
    mapping = self.ctx.convert.name_to_value("typing.Mapping")
    overlay_utils.add_base_class(node, cls, mapping)
