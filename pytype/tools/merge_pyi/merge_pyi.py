# Based on
# https://github.com/python/mypy/blob/master/misc/fix_annotate.py

"""Fixer that inserts type annotations from pyi files into methods.

Annotations are inserted either as comments or using the PEP484 syntax (requires
python3.5).

For example, if provided with a pyi containing

  def foo(self, bar: Any, baz: int) -> Any: ...

this transforms

  def foo(self, bar, baz=12):
      return bar + baz

into (comment annotation)

  def foo(self, bar, baz=12):
      # type: (Any, int) -> Any
      return bar + baz

or (PEP484 annotation)

  def foo(self, bar: Any, baz: int=12) -> Any:
      return bar + baz

The pyi is assumed to be generated by another tool.

When inserting annotations as comments, and the pyi has only partial
information, it uses some basic heuristics to decide when to ignore the first
argument of a class method:

  - always if it's named 'self'
  - if there's a @classmethod decorator

Finally, it knows that __init__() is supposed to return None.
"""

import collections
import itertools
import logging

from lib2to3 import pygram
from lib2to3 import pytree
# lib2to3.refactor is missing from typeshed
from lib2to3 import refactor  # pytype: disable=import-error
from lib2to3.fixer_base import BaseFix
from lib2to3.fixer_util import does_tree_import
from lib2to3.fixer_util import find_indentation
from lib2to3.fixer_util import syms
from lib2to3.fixer_util import token
from lib2to3.patcomp import compile_pattern
from lib2to3.pgen2 import driver
from lib2to3.pytree import Leaf
from lib2to3.pytree import Node

__all__ = ['KnownError',
           'FixMergePyi',
           'annotate_string']


def patch_grammar(grammar_file):
  """Patch in the given lib2to3 grammar."""
  grammar = driver.load_grammar(grammar_file)
  for name, symbol in pygram.python_grammar.symbol2number.items():
    delattr(pygram.python_symbols, name)
  for name, symbol in grammar.symbol2number.items():
    setattr(pygram.python_symbols, name, symbol)
  pygram.python_grammar = grammar


class KnownError(Exception):
  """Exceptions we already know about."""


class Util:
  """Utility functions for working with Nodes."""

  return_expr = compile_pattern("""return_stmt< 'return' any >""")

  @classmethod
  def has_return_exprs(cls, node):
    """Traverse the tree below node looking for 'return expr'.

    Args:
      node: The AST node at the root of the subtree.

    Returns:
      True if 'return' or 'return expr' is found, False otherwise.
    """
    results = {}
    if cls.return_expr.match(node, results):
      return True
    for child in node.children:
      if child.type not in (syms.funcdef, syms.classdef):
        if cls.has_return_exprs(child):
          return True
    return False

  driver = driver.Driver(
      pygram.python_grammar_no_print_statement, convert=pytree.convert)

  @classmethod
  def parse_string(cls, text):
    """Use lib2to3 to parse text into a Node."""

    text = text.strip()
    if not text:
      # cls.driver.parse_string just returns the ENDMARKER Leaf, wrap in
      # a Node for consistency
      return Node(syms.file_input, [Leaf(token.ENDMARKER, '')])

    # workaround: parsing text without trailing '\n' throws exception
    text += '\n'
    return cls.driver.parse_string(text)


class ArgSignature:
  """Partially parsed representation of a function argument."""

  def __init__(self, arg_nodes):
    sig = ArgSignature._split_arg(arg_nodes)
    (self._is_tuple, self._stars, self._arg_type, self._name_nodes,
     self._default) = sig
    self._was_modified = False

  @property
  def is_tuple(self):
    """Do we use the PEP 31113 packed-tuple syntax?"""
    return self._is_tuple

  @property
  def stars(self):
    """String: (''|'*'|'**')."""
    return self._stars

  @property
  def arg_type(self):
    """Existing annotation: (Node|Leaf|None)."""
    return self._arg_type

  @property
  def default(self):
    """Node holding default value or None."""
    return self._default

  @property
  def name(self):
    """Our name as a string. Throws if is_tuple (no reasonable name)."""
    assert not self.is_tuple
    n = self._name_nodes[-1]

    assert n.type == token.NAME, repr(n)
    return n.value

  @staticmethod
  def _split_arg(arg):
    """Splits function argument node list into a tuple.

    Args:
      arg: A list of nodes corresponding to a function argument.

    Raises:
      KnownError: Erroneous syntax was found.

    Returns:
      A tuple with the following components:
        is_tuple: bool, are we a packed-tuple arg
        stars: (''|'*'|'**')
        arg_type: (Node|Leaf|None) -- existing annotation
        name_nodes: NonEmptyList(Node|Leaf) -- argument name
        default: (Node|Leaf) -- default value
    """
    # in cpython, see ast_for_arguments in ast.c

    assert arg, 'Need non-empty list'
    arg = list(arg)

    is_tuple, stars, arg_type, default = False, '', None, None

    def is_leaf(n):
      return isinstance(n, Leaf)

    def get_unique_idx(nodes, test_set):
      """Get the index of the Leaf node that matches test_set, if one exists.

      Args:
        nodes: The list of nodes to search in. (The haystack.)
        test_set: The list of values to test for. (The needles.)

      Returns:
        The index of the unique Leaf node n where n.value is in test_set, or
        None if no such node exists.
      """
      matches = [
          i for i, n in enumerate(nodes) if is_leaf(n) and n.value in test_set
      ]
      assert len(matches) in (0, 1)
      return matches[0] if matches else None

    # [('*'|'**')] (NAME | packed_tuple) [':' test] ['=' test]

    # Strip stars
    idx = get_unique_idx(arg, ['*', '**'])
    if idx is not None:
      assert idx == 0
      stars = arg.pop(idx).value

    # Strip default
    idx = get_unique_idx(arg, '=')
    if idx is not None:
      assert idx == (len(arg) - 2)
      arg, default = arg[:idx], arg[idx + 1]

    def split_colon(nodes):
      idx = get_unique_idx(nodes, ':')
      if idx is None:
        return nodes, None
      assert idx == (len(nodes) - 2)
      return nodes[:idx], nodes[idx + 1]

    # Strip one flavor of arg_type (the other flavor, where we have a tname
    # Node, is handled below)
    arg, arg_type = split_colon(arg)

    if len(arg) == 3:
      assert arg[0].type == token.LPAR
      assert arg[2].type == token.RPAR
      assert arg[1].type in (syms.tfpdef, syms.tfplist)

      is_tuple = True

      assert stars == ''  # pylint: disable=g-explicit-bool-comparison
      assert arg_type is None  # type declaration goes inside tuple

      return is_tuple, stars, arg_type, arg, default

    if len(arg) != 1:
      if not arg and stars == '*':
        return is_tuple, stars, arg_type, arg, default
      raise KnownError()  # expected/parse_error.py

    node = arg[0]
    if is_leaf(node):
      return is_tuple, stars, arg_type, arg, default

    assert node.type in (syms.tname, syms.tfpdef)

    is_tuple = (node.type == syms.tfpdef)

    if node.type == syms.tname:
      arg, inner_arg_type = split_colon(node.children)
      if inner_arg_type is not None:
        assert arg_type is None
        arg_type = inner_arg_type

    return is_tuple, stars, arg_type, arg, default

  def insert_annotation(self, arg_type):
    """Modifies tree to set string arg_type as our type annotation."""
    # maybe the right way to do this is to insert as next child
    # in our parent instead? Or could replace self._arg[-1]
    # with modified version of itself
    assert self.arg_type is None, 'already annotated'
    assert not self._was_modified, 'can only set annotation once'
    self._was_modified = True

    name = self._name_nodes[-1]
    assert name.type == token.NAME

    typed_name = Node(syms.tname, [
        Leaf(token.NAME, self.name),
        Leaf(token.COLON, ':'),
        clean_clone(arg_type, False)
    ])

    typed_name.prefix = name.prefix

    name.replace(typed_name)


class FuncSignature:
  """A function or method."""

  _full_name: str

  # The pattern to match.
  PATTERN = """
              funcdef<
                'def' name=NAME
                parameters< '(' [args=any+] ')' >
                ['->' ret_annotation=any]
                colon=':' suite=any+ >
              """

  def __init__(self, node, match_results):
    """node must match PATTERN."""

    name = match_results.get('name')
    assert isinstance(name, Leaf), repr(name)
    assert name.type == token.NAME, repr(name)

    self._ret_type = match_results.get('ret_annotation')
    self._full_name = self._make_function_key(name)

    args = self._split_args(match_results.get('args'))
    self._arg_sigs = tuple(map(ArgSignature, args))

    self._node = node
    self._match_results = match_results
    self._inserted_ret_annotation = False

  def __str__(self):
    return self.full_name

  @property
  def full_name(self):
    """Fully-qualified name string."""
    return self._full_name

  @property
  def short_name(self):
    return self._match_results.get('name').value

  @property
  def ret_type(self):
    """Return type, Node? or None."""
    return self._ret_type

  @property
  def arg_sigs(self):
    """List[ArgSignature]."""
    return self._arg_sigs

  # The parse tree has a different shape when there is a single
  # decorator vs. when there are multiple decorators.
  decorated_pattern = compile_pattern("""
    decorated< (d=decorator | decorators< dd=decorator+ >) funcdef >
    """)

  @property
  def decorators(self):
    """A list of the function's decorators.

    This is a list of strings; only simple decorators (e.g. @staticmethod) are
    returned. If the function is undecorated or only non-simple decorators
    are found, return [].

    Returns:
      The names of the function's decorators as a list of strings. Only simple
      decorators (e.g. @staticmethod) are returned. If the function is not
      decorated or only non-simple decorators are found, return [].
    """
    node = self._node
    if node.parent is None:
      return []
    results = {}
    if not self.decorated_pattern.match(node.parent, results):
      return []
    decorators = results.get('dd') or [results['d']]
    decs = []
    for d in decorators:
      for child in d.children:
        if child.type == token.NAME:
          decs.append(child.value)
    return decs

  @property
  def is_method(self):
    """Whether we are (directly) inside a class."""
    node = self._node.parent
    while node is not None:
      if node.type == syms.classdef:
        return True
      if node.type == syms.funcdef:
        return False
      node = node.parent
    return False

  @property
  def has_return_exprs(self):
    """True if function has "return expr" anywhere."""
    return Util.has_return_exprs(self._node)

  @property
  def has_pep484_annotations(self):
    """Do we have any pep484 annotations?"""
    return self.ret_type or any(arg.arg_type for arg in self.arg_sigs)

  @property
  def has_comment_annotations(self):
    """Do we have any comment annotations?"""
    children = self._match_results['suite'][0].children
    for ch in children:
      if ch.prefix.lstrip().startswith('# type:'):
        return True

    return False

  def insert_ret_annotation(self, ret_type):
    """In-place annotation. Can only be called once."""
    assert not self._inserted_ret_annotation
    self._inserted_ret_annotation = True

    colon = self._match_results.get('colon')
    colon.prefix = ' -> ' + str(ret_type).strip() + colon.prefix

  def try_insert_comment_annotation(self, annotation):
    """Try to insert '# type: {annotation}' comment."""
    # For reference, see lib2to3/fixes/fix_tuple_params.py in stdlib.
    # "Compact" functions (e.g. 'def foo(x, y): return max(x, y)')
    # are not annotated.

    children = self._match_results['suite'][0].children
    if not (len(children) >= 2 and children[1].type == token.INDENT):
      return False  # can't annotate

    node = children[1]
    node.prefix = '%s# type: %s\n%s' % (node.value, annotation, node.prefix)
    node.changed()
    return True

  scope_pattern = compile_pattern("""(
    funcdef < 'def'   name=TOKEN any*> |
    classdef< 'class' name=TOKEN any*>
    )""")

  @classmethod
  def _make_function_key(cls, node):
    """Return the fully-qualified name of the function the node is under.

    If source is

    class C:
      def foo(self):
        x = 1

    We'll return 'C.foo' for any nodes related to 'x', '1', 'foo', 'self',
    and either 'C' or '' otherwise.

    Args:
      node: The node to start searching from.

    Returns:
      The function key as a string.
    """
    result = []
    while node is not None:
      match_result = {}
      if cls.scope_pattern.match(node, match_result):
        result.append(match_result['name'].value)

      node = node.parent

    return '.'.join(reversed(result))

  @staticmethod
  def _split_args(args):
    """Turns the match of PATTERN.args into a list of non-empty lists of nodes.

    Args:
      args: The value matched by PATTERN.args.

    Returns:
      A list of non-empty lists of nodes, where each list corresponds to a
      function argument.
    """
    if args is None:
      return []

    assert isinstance(args, list) and len(args) == 1, repr(args)

    args = args[0]
    if isinstance(args, Leaf) or args.type == syms.tname:
      args = [args]
    else:
      args = args.children

    return split_comma(args)


class FixMergePyi(BaseFix):
  """Specialized lib2to3 fixer for applying pyi annotations."""

  # This fixer is compatible with the bottom matcher.
  BM_compatible = True  # pylint: disable=invalid-name

  # This fixer shouldn't run by default.
  explicit = True

  PATTERN = FuncSignature.PATTERN

  def __init__(self, options, log):
    super().__init__(options, log)

    # name -> FuncSignature map obtained from .pyi file
    self.pyi_funcs = None

    self.inserted_types = []

    self.logger = logging.getLogger(self.__class__.__name__)

    # Options below

    # insert type annotations in PEP484 style. Otherwise insert as comments
    self._annotate_pep484 = False

  @property
  def annotate_pep484(self):
    return self._annotate_pep484

  @annotate_pep484.setter
  def annotate_pep484(self, value):
    self._annotate_pep484 = bool(value)

  def transform(self, node, results):
    assert self.pyi_funcs is not None, 'must provide function annotations'

    src_sig = FuncSignature(node, results)
    if not self.can_annotate(src_sig):
      return
    pyi_sig = self.pyi_funcs[src_sig.full_name]

    if self.annotate_pep484:
      self.inserted_types.extend(self.insert_annotation(src_sig, pyi_sig))
    else:
      self.inserted_types.extend(
          self.insert_comment_annotation(src_sig, pyi_sig))

  def insert_annotation(self, src_sig, pyi_sig):
    """Insert annotation in PEP484 format."""
    inserted_types = []
    for arg_sig, pyi_arg_sig in zip(src_sig.arg_sigs, pyi_sig.arg_sigs):
      if not pyi_arg_sig.arg_type:
        continue
      new_type = clean_clone(pyi_arg_sig.arg_type, False)
      arg_sig.insert_annotation(new_type)
      inserted_types.append(new_type)

    if pyi_sig.ret_type:
      src_sig.insert_ret_annotation(pyi_sig.ret_type)
      inserted_types.append(pyi_sig.ret_type)
    return inserted_types

  def insert_comment_annotation(self, src_sig, pyi_sig):
    """Insert function annotation as a comment string."""
    inserted_types = []
    str_arg_types = []
    for i, (arg_sig, pyi_arg_sig) in enumerate(
        zip(src_sig.arg_sigs, pyi_sig.arg_sigs)):
      is_first = (i == 0)
      new_type = clean_clone(pyi_arg_sig.arg_type, True)

      if new_type:
        new_type_str = str(new_type).strip()
        inserted_types.append(new_type)
      elif self.infer_should_annotate(src_sig, arg_sig, is_first):
        new_type_str = 'Any'
      else:
        continue

      str_arg_types.append(arg_sig.stars + new_type_str)

    ret_type = pyi_sig.ret_type
    if ret_type:
      inserted_types.append(ret_type)
    else:
      ret_type = self.infer_ret_type(src_sig)

    annot = '(' + ', '.join(str_arg_types) + ') -> ' + str(ret_type).strip()
    if src_sig.try_insert_comment_annotation(annot):
      if 'Any' in annot:
        inserted_types.append(Leaf(token.NAME, 'Any'))
      return inserted_types
    else:
      return []

  def can_annotate(self, src_sig):
    if (src_sig.short_name.startswith('__') and
        src_sig.short_name.endswith('__') and
        src_sig.short_name != '__init__' and
        src_sig.short_name != '__new__'):
      self.logger.info('magic method, skipping %s', src_sig)
      return False

    if src_sig.has_pep484_annotations or src_sig.has_comment_annotations:
      self.logger.warning('already annotated, skipping %s', src_sig)
      return False

    if src_sig.full_name not in self.pyi_funcs:
      self.logger.warning('no signature for %s, skipping', src_sig)
      return False

    pyi_sig = self.pyi_funcs[src_sig.full_name]

    if not pyi_sig.has_pep484_annotations:
      self.logger.warning('ignoring pyi definition with no annotations: %s',
                          pyi_sig)
      return False

    if not self.func_sig_compatible(src_sig, pyi_sig):
      self.logger.warning('incompatible annotation, skipping %s', src_sig)
      return False

    return True

  @staticmethod
  def func_sig_compatible(src_sig, pyi_sig):
    """Can src_sig be annotated with the info in pyi_sig?

    For the two signatures to be compatible, the number of arguments
    must match, they must have the same star args and they can't be tuple
    arguments.

    Args:
      src_sig: A FuncSignature representing the .py signature.
      pyi_sig: A FuncSignature representing the .pyi signature.

    Returns:
      True if the two signatures are compatible, False otherwise.
    """
    if len(pyi_sig.arg_sigs) != len(src_sig.arg_sigs):
      return False

    for pyi, cur in zip(pyi_sig.arg_sigs, src_sig.arg_sigs):
      # Entirely skip functions that use tuple args
      if cur.is_tuple or pyi.is_tuple:
        return False

      # Stars are expected to match
      if cur.stars != pyi.stars:
        return False

    return True

  @staticmethod
  def infer_ret_type(src_sig):
    """Heuristic for return type of a function."""
    if src_sig.short_name == '__init__' or not src_sig.has_return_exprs:
      return 'None'
    return 'Any'

  @staticmethod
  def infer_should_annotate(func, arg, at_start):
    """Heuristic for whether arg, in func, should be annotated."""

    if func.is_method and at_start and 'staticmethod' not in func.decorators:
      # Don't annotate the first argument if it's named 'self'.
      # Don't annotate the first argument of a class method.
      if arg.name == 'self' or 'classmethod' in func.decorators:
        return False

    return True

  def set_pyi_funcs(self, pyi_funcs):
    """Set the annotations the fixer will use."""
    self.pyi_funcs = pyi_funcs


class Pyi(collections.namedtuple('Pyi', 'imports assignments funcs')):
  """A parsed pyi."""

  def _get_imports(self, inserted_types):
    """Get the imports that provide the given types."""
    used_names = set()
    for node in inserted_types + self.assignments:
      for leaf in node.leaves():
        if leaf.type == token.NAME:
          used_names.add(leaf.value)
          # All prefixes are possible imports.
          while '.' in leaf.value:
            value, _ = leaf.rsplit('.', 1)
            used_names.add(value)
    for (pkg, pkg_alias), names in self.imports:
      if not names:
        if (pkg_alias or pkg) in used_names:
          yield ((pkg, pkg_alias), names)
      else:
        names = [(name, alias) for name, alias in names
                 if name == '*' or (alias or name) in used_names]
        if names:
          yield ((pkg, pkg_alias), names)

  def add_globals(self, tree, inserted_types):
    """Add required globals to the tree. Idempotent."""
    # Copy imports if not already present
    top_lines = []
    def import_name(name, alias):
      return name + ('' if alias is None else ' as %s' % alias)
    for (pkg, pkg_alias), names in self._get_imports(inserted_types):
      if not names:
        if does_tree_import(None, pkg_alias or pkg, tree):
          continue
        top_lines.append('import %s\n' % import_name(pkg, pkg_alias))
      else:
        assert pkg_alias is None
        import_names = []
        for name, alias in names:
          if does_tree_import(pkg, alias or name, tree):
            continue
          import_names.append(import_name(name, alias))
        if not import_names:
          continue
        top_lines.append('from %s import %s\n' % (pkg, ', '.join(import_names)))

    import_idx = [
        idx for idx, idx_node in enumerate(tree.children)
        if self.import_pattern.match(idx_node)
    ]
    if import_idx:
      insert_pos = import_idx[-1] + 1
    else:
      insert_pos = 0

      # first string (normally docstring)
      for idx, idx_node in enumerate(tree.children):
        if (idx_node.type == syms.simple_stmt and idx_node.children and
            idx_node.children[0].type == token.STRING):
          insert_pos = idx + 1
          break

    if self.assignments:
      top_lines.append('\n')
      top_lines.extend(str(a).strip() + '\n' for a in self.assignments)
    top_lines = Util.parse_string(''.join(top_lines))
    for offset, offset_node in enumerate(top_lines.children[:-1]):
      tree.insert_child(insert_pos + offset, offset_node)

  @classmethod
  def _log_warning(cls, *args):
    logger = logging.getLogger(cls.__name__)
    logger.warning(*args)

  @classmethod
  def parse(cls, text):
    """Parse .pyi string, return as Pyi."""
    tree = Util.parse_string(text)

    funcs = {}
    for node, match_results in generate_matches(tree, cls.function_pattern):
      sig = FuncSignature(node, match_results)

      if sig.full_name in funcs:
        cls._log_warning('Ignoring redefinition: %s', sig)
      else:
        funcs[sig.full_name] = sig

    imports = []
    # Any is sometimes inserted as a default type, so make sure typing.Any is
    # always importable.
    any_import = False
    for node, match_results in generate_top_matches(tree, cls.import_pattern):
      pkg, names = cls.parse_top_import(match_results)
      if pkg == ('typing', None) and names:
        if ('Any', None) not in names:
          names.insert(0, ('Any', None))
        any_import = True
      imports.append((pkg, names))
    if not any_import:
      imports.append((('typing', None), [('Any', None)]))

    assignments = []
    for node, match_results in generate_top_matches(tree, cls.assign_pattern):
      text = str(node)

      # hack to avoid shadowing real variables -- proper solution is more
      # complicated, use util.find_binding
      if 'TypeVar' in text or (text and text[0] == '_'):
        assignments.append(node)
      else:
        cls._log_warning('ignoring %s', repr(text))

    return cls(tuple(imports), tuple(assignments), funcs)

  function_pattern = compile_pattern(FuncSignature.PATTERN)

  assign_pattern = compile_pattern("""
    simple_stmt< expr_stmt<any+> any* >
    """)

  import_pattern = compile_pattern("""
    simple_stmt<
        ( import_from< 'from' pkg=any+ 'import' ['('] names=any [')'] > |
          import_name< 'import' pkg=any+ > )
        any*
    >
    """)

  @classmethod
  def _parse_import_alias(cls, leaves):
    assert leaves[-2].value == 'as'
    name = ''.join(leaf.value for leaf in leaves[:-2])
    return (name, leaves[-1].value)

  @classmethod
  def parse_top_import(cls, results):
    """Splits the result of import_pattern into component strings.

    Examples:

    'from pkg import a,b,c' gives
    (('pkg', None), [('a', None), ('b', None), ('c', None)])

    'import pkg' gives
    (('pkg', None), [])

    'from pkg import a as b' gives
    (('pkg', None), [('a', 'b')])

    'import pkg as pkg2' gives
    (('pkg', 'pkg2'), [])

    'import pkg.a as b' gives
    (('pkg.a', 'b'), [])

    Args:
      results: The values from import_pattern.

    Returns:
      A tuple of the package name and the list of imported names. Each name is a
      tuple of original name and alias.
    """

    pkg, names = results['pkg'], results.get('names', None)

    if len(pkg) == 1 and pkg[0].type == pygram.python_symbols.dotted_as_name:
      pkg_out = cls._parse_import_alias(list(pkg[0].leaves()))
    else:
      pkg_out = (''.join(map(str, pkg)).strip(), None)

    names_out = []
    if names:
      names = split_comma(names.leaves())
      for name in names:
        if len(name) == 1:
          assert name[0].type in (token.NAME, token.STAR)
          names_out.append((name[0].value, None))
        else:
          names_out.append(cls._parse_import_alias(name))

    return pkg_out, names_out


class StandaloneRefactoringTool(refactor.RefactoringTool):
  """Modified RefactoringTool for running outside the standard 2to3 install."""

  def __init__(self, options):
    self._fixer = None
    super().__init__([], options=options)

  def get_fixers(self):
    if self.fixer.order == 'pre':
      return [self.fixer], []
    else:
      return [], [self.fixer]

  @property
  def fixer(self):
    if not self._fixer:
      self._fixer = FixMergePyi(self.options, self.fixer_log)
    return self._fixer


def is_top_level(node):
  """Is node at top indentation level (i.e. module globals)?"""
  return bool(len(find_indentation(node)))


def generate_matches(tree, pattern):
  """Generator yielding nodes in tree that match pattern."""
  for node in tree.pre_order():
    results = {}
    if pattern.match(node, results):
      yield node, results


def generate_top_matches(node, pattern):
  """Generator yielding direct children of node that match pattern."""
  for child in node.children:
    results = {}
    if pattern.match(child, results):
      yield child, results


def clean_clone(node, strip_formatting):
  """Clone node so it can be inserted in a tree. Optionally strip formatting."""
  if not node:
    return None

  if strip_formatting:
    # strip formatting and comments, represent as prettyfied string
    # For comment-style annotations, important to have a single line
    s = ''.join(
        ', ' if token.COMMA == n.type else n.value for n in node.leaves())
    assert s

    # parse back into a Node
    node = Util.parse_string(s)
    assert len(node.children) == 2
    node = node.children[0]
  else:
    node = node.clone()

  node.parent = None

  return node


def split_comma(nodes):
  """Take iterable of nodes, return list of lists of nodes."""

  def is_comma(n):
    return token.COMMA == n.type

  groups = itertools.groupby(nodes, is_comma)
  return [list(group) for comma, group in groups if not comma]


def annotate_string(args, py_src, pyi_src):
  """Applies the annotations in pyi_src to py_src."""

  tool = StandaloneRefactoringTool(options={'print_function': True})
  fixer = tool.fixer

  fixer.annotate_pep484 = not args.as_comments
  parsed_pyi = Pyi.parse(pyi_src)
  fixer.set_pyi_funcs(parsed_pyi.funcs)

  tree = tool.refactor_string(py_src + '\n', '<inline>')
  parsed_pyi.add_globals(tree, tuple(fixer.inserted_types))

  annotated_src = str(tree)[:-1]

  return annotated_src
