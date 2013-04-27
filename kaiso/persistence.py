from kaiso.attributes import Outgoing, Incoming, String, Uuid
from kaiso.attributes.bases import get_attibute_for_type
from kaiso.connection import get_connection
from kaiso.exceptions import (
    UniqueConstraintError, DeserialisationError, UnknownType)
from kaiso.iter_helpers import unique
from kaiso.references import set_store_for_object
from kaiso.relationships import InstanceOf, IsA, DeclaredOn
from kaiso.types import (
    Descriptor,
    Persistable, PersistableMeta, Relationship, Attribute,
    AttributedBase, get_indexes, get_index_name,
    is_indexable)
from py2neo import cypher, neo4j


class TypeSystem(AttributedBase):
    id = String(unique=True)
    version = Uuid()


class Defines(Relationship):
    pass


def join_lines(*lines, **kwargs):
    rows = []
    sep = kwargs.get('sep', '\n')

    for lne in lines:
        if isinstance(lne, tuple):
            (lne, s) = lne
            lne = '    ' + join_lines(sep=s+'\n    ', *lne)
        rows.append(lne)
    return sep.join(rows)


def object_to_dict(obj):
    """ Converts a persistable object to a dict.

    The generated dict will contain a __type__ key, for which the value will
    be the type_id as given by the descriptor for type(obj).

    If the object is a class a name key-value pair will be
    added to the generated dict, with the value being the type_id given
    by the descriptor for the object.

    For any other object all the attributes as given by the object's
    type descriptpr will be added to the dict and encoded as required.

    Args:
        obj: A persistable  object.

    Returns:
        Dictionary with attributes encoded in basic types
        and type information for deserialization.
        e.g.
        {
            '__type__': 'Entity',
            'attr1' : 1234
        }
    """
    obj_type = type(obj)

    properties = {
        '__type__': Descriptor(obj_type).type_id,
    }

    if isinstance(obj, type):
        properties['id'] = Descriptor(obj).type_id

    elif isinstance(obj, Attribute):
        properties['unique'] = obj.unique

    else:
        descr = Descriptor(obj_type)

        for name, attr in descr.attributes.items():
            value = attr.to_db(getattr(obj, name))
            if value is not None:
                properties[name] = value

    return properties


def dict_to_object(properties, dynamic_type=PersistableMeta):
    """ Converts a dict into a persistable object.

    The properties dict needs at least a __type__ key containing the name of
    any registered class.
    The type key defines the type of the object to return.

    If the registered class for the __type__ is a meta-class,
    i.e. a subclass of <type>, a name key is assumed to be present and
    the registered class idendified by it's value is returned.

    If the registered class for the __type__ is standard class,
    i.e. an instance of <type>, and object of that class will be created
    with attributes as defined by the remaining key-value pairs.

    Args:
        properties: A dict like object.

    Returns:
        A persistable object.
    """

    try:
        type_id = properties['__type__']
    except KeyError:
        raise DeserialisationError(
            'properties "{}" missing __type__ key'.format(properties))

    if type_id == Descriptor(PersistableMeta).type_id:
        # we are looking at a class object
        cls_id = properties['id']
    else:
        # we are looking at an instance object
        cls_id = type_id

    try:
        cls = dynamic_type.get_class_by_id(cls_id)
    except UnknownType:
        cls = PersistableMeta.get_class_by_id(cls_id)

    if cls_id != type_id:
        return cls
    else:
        obj = cls.__new__(cls)

        if isinstance(obj, Attribute):
            for attr_name, value in properties.iteritems():
                setattr(obj, attr_name, value)
        else:
            descr = Descriptor(cls)

            for attr_name, attr in descr.attributes.items():
                try:
                    value = properties[attr_name]
                except KeyError:
                    value = attr.default
                else:
                    value = attr.to_python(value)

                setattr(obj, attr_name, value)

    return obj


def object_to_db_value(obj):
    try:
        attr_cls = get_attibute_for_type(type(obj))
    except KeyError:
        return obj
    else:
        return attr_cls.to_db(obj)


def dict_to_db_values_dict(data):
    return {k: object_to_db_value(v) for k, v in data.items()}


@unique
def get_type_relationships(obj):
    """ Generates a list of the type relationships of an object.
    e.g.
        get_type_relationships(Entity())

        (object, InstanceOf, type),
        (type, IsA, object),
        (type, InstanceOf, type),
        (PersistableMeta, IsA, type),
        (PersistableMeta, InstanceOf, type),
        (Entity, IsA, object),
        (Entity, InstanceOf, PersistableMeta),
        (<Entity object>, InstanceOf, Entity)

    Args:
        obj:    An object to generate the type relationships for.

    Returns:
        A generator, generating tuples
            (object, relatsionship type, related obj)
    """
    obj_type = type(obj)

    if obj_type is not type:
        for item in get_type_relationships(obj_type):
            yield item

    if isinstance(obj, type):
        for base in obj.__bases__:
            for item in get_type_relationships(base):
                yield item
            yield obj, IsA, base

    yield obj, InstanceOf, obj_type


def get_index_filter(obj):
    indexes = get_indexes(obj)
    index_filter = {key: value for _, key, value in indexes}
    return index_filter


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


def get_create_types_query(obj, root):
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
        'IsA_props': object_to_dict(IsA(None, None)),
        'Defines_props': object_to_dict(Defines(None, None)),
        'InstanceOf_props': object_to_dict(InstanceOf(None, None)),
        'DeclaredOn_props': object_to_dict(DeclaredOn(None, None))
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

            for attr_name, attr in descriptor.declared_attributes.iteritems():
                key = "%s_%s" % (name1, attr_name)
                ln = '({%s}) -[:DECLAREDON {%s_props}]-> %s' % (
                    key, DeclaredOn.__name__, name1
                )
                lines.append(ln)

                attr_dict = object_to_dict(attr)
                attr_dict['name'] = attr_name
                query_args[key] = attr_dict

    for key, obj in objects.iteritems():
        query_args['%s_props' % key] = object_to_dict(obj)

    query = join_lines(
        'START root=node:%s(id={root_id})' % get_index_name(type(root)),
        'CREATE UNIQUE',
        (lines, ','),
        'RETURN %s' % ', '.join(objects.keys())
    )
    return query, objects.values(), query_args


def get_create_relationship_query(rel):
    rel_props = object_to_dict(rel)
    query = 'START %s, %s CREATE n1 -[r:%s {props}]-> n2 RETURN r'

    query = query % (
        get_start_clause(rel.start, 'n1'),
        get_start_clause(rel.end, 'n2'),
        rel_props['__type__'].upper(),
    )

    return query


def _get_changes(old, new):
    """Return a changes dictionary containing the key/values in new that are
       different from old. Any key in old that is not in new will have a None
       value in the resulting dictionary
    """
    changes = {}

    # check for any keys that have changed, put their new value in
    for key, value in new.items():
        if old.get(key) != value:
            changes[key] = value

    # if a key has dissappeared in new, put a None in changes, which
    # will remove it in neo
    for key in old.keys():
        if key not in new:
            changes[key] = None

    return changes


class Storage(object):
    """ Provides a queryable object store.

    The object store can store any object as long as it's type is registered.
    This includes instances of Entity, PersistableMeta
    and subclasses of either.

    InstanceOf and IsA relationships are automatically generated,
    when persisting an object.
    """
    def __init__(self, connection_uri):
        """ Initializes a Storage object.

        Args:
            connection_uri: A URI used to connect to the graph database.
        """
        self._conn = get_connection(connection_uri)
        self.type_system = TypeSystem(id='TypeSystem')

        self.dynamic_type = type(
            PersistableMeta.__name__, (PersistableMeta,), {})

    def _execute(self, query, **params):
        """ Runs a cypher query returning only raw rows of data.

        Args:
            query: A parameterized cypher query.
            params: The parameters used by the query.

        Returns:
            A generator with the raw rows returned by the connection.
        """
        print '-------------------'
        print query.format(**params)

        rows, _ = cypher.execute(self._conn, query, params)
        for row in rows:
            yield row

    def _convert_value(self, value):
        """ Converts a py2neo primitive(Node, Relationship, basic object)
        to an equvalent python object.
        Any value which cannot be converted, will be returned as is.

        Args:
            value: The value to convert.

        Returns:
            The converted value.
        """
        if isinstance(value, (neo4j.Node, neo4j.Relationship)):
            properties = value.get_properties()
            obj = dict_to_object(properties, self.dynamic_type)

            if isinstance(value, neo4j.Relationship):
                obj.start = self._convert_value(value.start_node)
                obj.end = self._convert_value(value.end_node)
            else:
                set_store_for_object(obj, self)
            return obj
        return value

    def _convert_row(self, row):
        for value in row:
            yield self._convert_value(value)

    def _index_object(self, obj, node_or_rel):
        indexes = get_indexes(obj)
        for index_name, key, value in indexes:
            if isinstance(obj, Relationship):
                index_type = neo4j.Relationship
            else:
                index_type = neo4j.Node

            index = self._conn.get_or_create_index(index_type, index_name)
            index.add(key, value, node_or_rel)

        if not isinstance(obj, Relationship):
            set_store_for_object(obj, self)

    def _add_types(self, cls):
        query, objects, query_args = get_create_types_query(
            cls, self.type_system)

        nodes_or_rels = next(self._execute(query, **query_args))

        for obj in objects:
            if is_indexable(obj):
                index_name = get_index_name(obj)
                self._conn.get_or_create_index(neo4j.Node, index_name)

        for obj, node_or_rel in zip(objects, nodes_or_rels):
            self._index_object(obj, node_or_rel)

        return cls

    def _add(self, obj):
        """ Adds an object to the data store.

        It will automatically generate the type relationships
        for the the object as required and store the object itself.
        """

        query_args = {}

        if isinstance(obj, PersistableMeta):
            # object is a type; create the type and its hierarchy
            return self._add_types(obj)

        elif obj is self.type_system:
            query = 'CREATE (n {props}) RETURN n'

        elif isinstance(obj, Relationship):
            # object is a relationship
            obj_type = type(obj)
            self._add_types(obj_type)
            query = get_create_relationship_query(obj)

        else:
            # object is an instance; create its type, its hierarchy and then
            # create the instance
            obj_type = type(obj)
            self._add_types(obj_type)

            idx_name = get_index_name(type(obj_type))
            query = (
                'START cls=node:%s(id={type_id}) '
                'CREATE (n {props}) -[:INSTANCEOF {rel_props}]-> cls '
                'RETURN n'
            ) % idx_name

            query_args = {
                'type_id': obj_type.__name__,
                'rel_props': object_to_dict(InstanceOf(None, None)),
            }

        query_args['props'] = object_to_dict(obj)

        (node_or_rel,) = next(self._execute(query, **query_args))

        self._index_object(obj, node_or_rel)

        # TODO: really?
        #if obj is self.type_system:
        #    self._add_types(type(obj))

        return obj

    def save(self, persistable):
        """ Stores the given ``persistable`` in the graph database.
        If a matching object (by unique keys) already exists, it will
        update it with the modified attributes.
        """
        if not can_add(persistable):
            raise TypeError('cannot persist %s' % persistable)

        existing = self.get(type(persistable), **get_index_filter(persistable))

        if existing is None:
            return self._add(persistable)

        existing_props = object_to_dict(existing)
        props = object_to_dict(persistable)

        if existing_props == props:
            # no changes
            return existing

        changes = _get_changes(old=existing_props, new=props)
        for (_, index_attr, _) in get_indexes(existing):
            if index_attr in changes:
                raise NotImplementedError(
                    "We currently don't support changing unique attributes")

        start_clause = get_start_clause(existing, 'n')

        set_clauses = ', '.join(['n.%s={%s}' % (key, key) for key in changes])

        query = join_lines(
            'START %s' % start_clause,
            'SET %s' % set_clauses,
            'RETURN n'
        )

        result = self._execute(query, **changes)
        return next(result)[0]

    def get(self, cls, **index_filter):
        index_filter = dict_to_db_values_dict(index_filter)

        query_args = {}

        indexes = index_filter.items()
        if len(indexes) == 0:
            return None

        if issubclass(cls, (Relationship, PersistableMeta)):
            idx_name = get_index_name(cls)
            idx_key, idx_value = indexes[0]

            if issubclass(cls, Relationship):
                self._conn.get_or_create_index(neo4j.Relationship, idx_name)
                start_func = 'relationship'
            else:
                self._conn.get_or_create_index(neo4j.Node, idx_name)
                start_func = 'node'

            query = 'START nr = %s:%s(%s={idx_value}) RETURN nr' % (
                start_func, idx_name, idx_key)

            query_args['idx_value'] = idx_value
        else:
            idx_where = []
            for key, value in indexes:
                idx_where.append('n.%s? = {%s}' % (key, key))
                query_args[key] = value
            idx_where = ' or '.join(idx_where)

            idx_name = get_index_name(TypeSystem)
            query = join_lines(
                'START root=node:%s(id={idx_value})' % idx_name,
                'MATCH n -[:INSTANCEOF]-> () -[:ISA*]-> () <-[:DEFINES]- root',
                'WHERE %s' % idx_where,
                'RETURN n',
            )

            query_args['idx_value'] = self.type_system.id

        found = [node for (node,) in self._execute(query, **query_args)]

        if not found:
            return None

        # all the nodes returned should be the same
        first = found[0]
        for node in found:
            if node.id != first.id:
                raise UniqueConstraintError((
                    "Multiple nodes ({}) found for unique lookup for "
                    "{}").format(found, cls))

        obj = self._convert_value(first)
        return obj

    def get_related_objects(self, rel_cls, ref_cls, obj):

        if ref_cls is Outgoing:
            rel_query = 'n -[:{}]-> related'
        elif ref_cls is Incoming:
            rel_query = 'n <-[:{}]- related'

        # TODO: should get the rel name from descriptor?
        rel_query = rel_query.format(rel_cls.__name__.upper())

        query = 'START {idx_lookup} MATCH {rel_query} RETURN related'

        query = query.format(
            idx_lookup=get_start_clause(obj, 'n'),
            rel_query=rel_query
        )

        rows = self.query(query)
        related_objects = (related_obj for (related_obj,) in rows)

        return related_objects

    def delete(self, obj):
        """ Deletes an object from the store.

        Args:
            obj: The object to delete.
        """
        if isinstance(obj, Relationship):
            query = join_lines(
                'START {}, {}',
                'MATCH n1 -[rel]-> n2',
                'DELETE rel'
            ).format(
                get_start_clause(obj.start, 'n1'),
                get_start_clause(obj.end, 'n2'),
            )
        elif isinstance(obj, PersistableMeta):
            query = join_lines(
                'START {}',
                'MATCH attr -[:DECLAREDON]-> obj',
                'DELETE attr',
                'MATCH obj -[rel]- ()',
                'DELETE obj, rel'
            ).format(
                get_start_clause(obj, 'obj')
            )
        else:
            query = join_lines(
                'START {}',
                'MATCH obj -[rel]- ()',
                'DELETE obj, rel'
            ).format(
                get_start_clause(obj, 'obj')
            )

        # TODO: delete node/rel from indexes

        cypher.execute(self._conn, query)

    def query(self, query, **params):
        """ Queries the store given a parameterized cypher query.

        Args:
            query: A parameterized cypher query.
            params: query: A parameterized cypher query.

        Returns:
            A generator with tuples containing stored objects or values.
        """
        params = dict_to_db_values_dict(params)
        for row in self._execute(query, **params):
            yield tuple(self._convert_row(row))

    def delete_all_data(self):
        """ Removes all nodes, relationships and indexes in the store.

            WARNING: This will destroy everything in your Neo4j database.
        """
        self._conn.clear()
        for index_name in self._conn.get_indexes(neo4j.Node).keys():
            self._conn.delete_index(neo4j.Node, index_name)
        for index_name in self._conn.get_indexes(neo4j.Relationship).keys():
            self._conn.delete_index(neo4j.Relationship, index_name)

    def initialize(self):
        idx_name = get_index_name(TypeSystem)
        self._conn.get_or_create_index(neo4j.Node, idx_name)
        self.save(self.type_system)


def can_add(obj):
    """ Returns True if obj can be added to the db.

        We can add instances of Entity or Relationship.
        In addition it is also possible to add sub-classes of
        Entity.
    """
    return isinstance(obj, Persistable)
