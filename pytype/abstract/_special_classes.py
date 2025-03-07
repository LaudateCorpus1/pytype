"""Classes that need special handling, typically due to code generation."""

from pytype.abstract import class_mixin
from pytype.pytd import pytd


class _Builder:
  """Build special classes created by inheriting from a specific class."""

  def __init__(self, ctx):
    self.ctx = ctx
    self.convert = ctx.convert

  def matches_class(self, c):
    raise NotImplementedError()

  def matches_base(self, c):
    raise NotImplementedError()

  def matches_mro(self, c):
    raise NotImplementedError()

  def make_base_class(self):
    raise NotImplementedError()

  def make_derived_class(self, name, pytd_cls):
    raise NotImplementedError()

  def maybe_build_from_pytd(self, name, pytd_cls):
    if self.matches_class(pytd_cls):
      return self.make_base_class()
    elif self.matches_base(pytd_cls):
      return self.make_derived_class(name, pytd_cls)
    else:
      return None

  def maybe_build_from_mro(self, abstract_cls, name, pytd_cls):
    if self.matches_mro(abstract_cls):
      return self.make_derived_class(name, pytd_cls)
    return None


class _TypedDictBuilder(_Builder):
  """Build a typed dict."""

  CLASSES = ("typing.TypedDict", "typing_extensions.TypedDict")

  def matches_class(self, c):
    return c.name in self.CLASSES

  def matches_base(self, c):
    return any(isinstance(b, pytd.ClassType) and self.matches_class(b)
               for b in c.bases)

  def matches_mro(self, c):
    # Check if we have typed dicts in the MRO by seeing if we have already
    # created a TypedDictClass for one of the ancestor classes.
    return any(isinstance(b, class_mixin.Class) and b.is_typed_dict_class
               for b in c.mro)

  def make_base_class(self):
    return self.convert.make_typed_dict_builder(self.ctx)

  def make_derived_class(self, name, pytd_cls):
    return self.convert.make_typed_dict(name, pytd_cls, self.ctx)


_BUILDERS = (_TypedDictBuilder,)


def maybe_build_from_pytd(name, pytd_cls, ctx):
  """Try to build a special class from a pytd class."""
  for b in _BUILDERS:
    ret = b(ctx).maybe_build_from_pytd(name, pytd_cls)
    if ret:
      return ret
  return None


def maybe_build_from_mro(abstract_cls, name, pytd_cls, ctx):
  """Try to build a special class from the MRO of an abstract class."""
  for b in _BUILDERS:
    ret = b(ctx).maybe_build_from_mro(abstract_cls, name, pytd_cls)
    if ret:
      return ret
  return None
