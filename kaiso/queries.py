
from kaiso.relationships import InstanceOf, IsA, DeclaredOn, Defines
from kaiso.types import Descriptor, AttributedBase, get_indexes, get_index_name
from kaiso.serialize import get_type_relationships, object_to_dict


def join_lines(*lines, **kwargs):
    rows = []
    sep = kwargs.get('sep', '\n')

    for lne in lines:
        if isinstance(lne, tuple):
            (lne, s) = lne
            lne = '    ' + join_lines(sep=s+'\n    ', *lne)

        if lne != '':
            rows.append(lne)

    return sep.join(rows)


def get_start_clause(obj, name):
    """ Returns a node lookup by index as used by the START clause.

    Args:
        obj: An object to create an index lookup.
        name: The name of the object in the query.
    Returns:
        A string with index lookup of a cypher START clause.
    """

    index = next(get_indexes(obj), None)
    query = '{}=node:{}({}="{}")'.format(name, *index)
    return query


def get_create_types_query(obj, root, dynamic_type):
    """ Returns a CREATE UNIQUE query for an entire type hierarchy.

    Includes statements that create each type's attributes.

    Args:
        obj: An object to create a type hierarchy for.

    Returns:
        A tuple containing:
        (cypher query, objects to create nodes for, the object names).
    """
    lines = []
    objects = {}

    query_args = {
        'root_id': root.id,
        'IsA_props': object_to_dict(IsA(None, None), dynamic_type),
        'Defines_props': object_to_dict(
            Defines(None, None), dynamic_type),
        'InstanceOf_props': object_to_dict(
            InstanceOf(None, None), dynamic_type),

    }

    is_first = True
    for cls1, rel_cls, cls2 in get_type_relationships(obj):
        # this filters out the types that we don't want to persist
        if issubclass(cls2, AttributedBase):
            name1 = cls1.__name__

            if name1 in objects:
                abstr1 = name1
            else:
                abstr1 = '(%s {%s_props})' % (name1, name1)

            objects[name1] = cls1

            if is_first:
                is_first = False
                ln = 'root -[:DEFINES {Defines_props}]-> %s' % abstr1
            else:
                name2 = cls2.__name__
                objects[name2] = cls2

                rel_name = rel_cls.__name__
                rel_type = rel_name.upper()

                ln = '%s -[:%s {%s_props}]-> %s' % (
                    abstr1, rel_type, rel_name, name2)
            lines.append(ln)

            # TODO: really?
            # if cls1 is type(root):
            #     ln = 'root -[:INSTANCEOF {InstanceOf_props}]-> %s' % (
            #         cls1.__name__)
            #     lines.append(ln)

            # create cls1's attributes
            descriptor = Descriptor(cls1)

            attributes = descriptor.declared_attributes
            for attr_name, attr in attributes.iteritems():
                key = "%s_%s" % (name1, attr_name)
                decl_key = "%s_decl_props" % key

                ln = '({%s}) -[:DECLAREDON {%s}]-> %s' % (
                    key, decl_key, name1)
                lines.append(ln)

                attr_dict = object_to_dict(
                    attr, dynamic_type, include_none=False)

                query_args[key] = attr_dict
                query_args[decl_key] = object_to_dict(
                    DeclaredOn(None, None, attr_name), dynamic_type)

    for key, obj in objects.iteritems():
        query_args['%s_props' % key] = object_to_dict(obj, dynamic_type)

    query = join_lines(
        'START root=node:%s(id={root_id})' % get_index_name(type(root)),
        'CREATE UNIQUE',
        (lines, ','),
        'RETURN %s' % ', '.join(objects.keys())
    )
    return query, objects.values(), query_args


def get_create_relationship_query(rel, dynamic_type):
    rel_props = object_to_dict(rel, dynamic_type, include_none=False)
    query = 'START %s, %s CREATE n1 -[r:%s {props}]-> n2 RETURN r'

    query = query % (
        get_start_clause(rel.start, 'n1'),
        get_start_clause(rel.end, 'n2'),
        rel_props['__type__'].upper(),
    )

    return query
