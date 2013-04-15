from functools import wraps
from weakref import WeakKeyDictionary, WeakValueDictionary

from py2neo import cypher, neo4j

from orp.connection import get_connection
from orp.descriptors import (
    get_descriptor, get_named_descriptor, get_index_name)
from orp.query import encode_query_values
from orp.types import PersistableType, Persistable
from orp.relationships import Relationship, InstanceOf, IsA


def first(items):
    ''' Returns the first item of an iterable object.

    Args:
        items: An iterable object

    Returns:
        The first item from items.
    '''
    return iter(items).next()


def get_indexes(obj):
    ''' Returns indexes for a persistable object.
        See orp.types for a definition of persistable types.

    Args:
        obj: A persistable object.

    Returns:
        Tuples (index_name, key, value) which can be used to index an object.
    '''

    obj_type = type(obj)

    if isinstance(obj, type):
        index_name = get_index_name(obj_type)
        value = get_descriptor(obj).type_name
        yield (index_name, 'name', value)
    else:
        descr = get_descriptor(obj_type)

        for name, attr in descr.members.items():
            if attr.unique:
                index_name = get_index_name(attr.declared_on)
                key = name
                value = attr.to_db(getattr(obj, name))
                yield (index_name, key, value)


def object_to_dict(obj):
    ''' Returns a basic dictionary containing all properties
    necessary to persist the object passed in.

    Args:
        obj: A persistable  object.

    Returns:
        Dictionary with attributes encoded in basic types
        and type information for deserialization.
        e.g.
        {
            '__type__': 'Persistable',
            'attr1' : 1234
        }
    '''
    obj_type = type(obj)

    descr = get_descriptor(obj_type)

    properties = {
        '__type__': descr.type_name,
    }

    if isinstance(obj, type):
        properties['name'] = get_descriptor(obj).type_name
    else:
        for name, attr in descr.members.items():
            value = attr.to_db(getattr(obj, name))
            if value is not None:
                properties[name] = value
    return properties


def dict_to_object(properties):
    # TODO: doc

    type_name = properties['__type__']
    descriptor = get_named_descriptor(type_name)

    cls = descriptor.cls

    if issubclass(cls, type):
        obj = get_named_descriptor(properties['name']).cls
    else:
        obj = cls.__new__(cls)

        for attr_name, attr in descriptor.members.items():
            try:
                value = properties[attr_name]
            except KeyError:
                value = attr.default
            else:
                value = attr.to_python(value)

            setattr(obj, attr_name, value)

    return obj


def unique(fn):
    ''' Wraps a function to return only unique items.
    The wrapped function must return an iterable object.
    When the wrapped function is called, each item from the iterable
    will be yielded only once and duplicates will be ignored.

    Args:
        fn: The function to be wrapped.

    Returns:
        A wrapper function for fn.
    '''
    @wraps(fn)
    def wrapped(*args, **kwargs):
        items = set()
        for item in fn(*args, **kwargs):
            if item not in items:
                items.add(item)
                yield item
    return wrapped


@unique
def get_type_relationships(obj):
    ''' Generates a list of the type relationships of an object.
    e.g.
        get_type_relationships(Persistable())

        (object, InstanceOf, type),
        (type, IsA, object),
        (type, InstanceOf, type),
        (PersistableType, IsA, type),
        (PersistableType, InstanceOf, type),
        (Persistable, IsA, object),
        (Persistable, InstanceOf, PersistableType),
        (<Persistable object>, InstanceOf, Persistable)

    Args:
        obj:    An object to generate the type relationships for.

    Returns:
        A generator, generating tuples
            (object, relatsionship type, related obj)
    '''
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


def get_index_query(obj, name=None):
    ''' Returns a node lookup by index as used by the START clause.

    Args:
        obj: An object to create a index lookup.
        name: The name of the object in the query.
                If name is None obj.__name__ will be used.
    Returns:
        A string with index lookup of a cypher START clause.
    '''

    if name is None:
        name = obj.__name__

    index_name, key, value = first(get_indexes(obj))

    query = '%s = node:%s(%s="%s")' % (name, index_name, key, value)
    return query


def get_create_types_query(obj):
    ''' Returns a CREATE UNIQUE query for an entire type hierarchy.

    Args:
        obj: An object to create a type hierarchy for.

    Returns:
        A tuple containing:
        (cypher query, objects to create nodes for, the object names).
    '''

    lines = []
    # we don't have a good starting node, so we just use PersistableType
    # see the add() method, which ensures it exists.
    objects = {'PersistableType': PersistableType}

    for obj1, rel_cls, obj2 in get_type_relationships(obj):
        # this filters out the types, which we don't want to persist
        if is_persistable_not_rel(obj1) and is_persistable_not_rel(obj2):
            if isinstance(obj1, type):
                name1 = obj1.__name__
            else:
                name1 = 'new_obj'

            name2 = obj2.__name__

            if name1 in objects:
                abstr1 = name1
            else:
                abstr1 = '(%s {%s_props})' % (name1, name1)

            if name2 in objects:
                abstr2 = name2
            else:
                abstr2 = '(%s {%s_props})' % (name2, name2)

            objects[name1] = obj1
            objects[name2] = obj2

            rel_name = rel_cls.__name__
            rel_type = rel_name.upper()

            ln = '%s -[:%s {%s_props}]-> %s' % (
                abstr1, rel_type, rel_name, abstr2)

            lines.append(ln)

    keys, objects = zip(*objects.items())

    query = (
        'START %s' % get_index_query(PersistableType),
        'CREATE UNIQUE')
    query += ('    ' + ',\n    '.join(lines),)
    query += ('RETURN %s' % ', '.join(keys),)
    query = '\n'.join(query)

    return query, objects, keys


class Storage(object):
    ''' Provides a queryable object store.

    The object store can store any object as long as it's type is registered.
    This includes instances of Persistable, PersistableType
    and subclasses of either.

    InstanceOf and IsA relationships are automatically generated,
    when persisting an object.
    '''
    def __init__(self, connection_uri):
        ''' Initializes a Storage object.

        Args:
            connection_uri: A URI used to connect to the graph database.
        '''
        self._conn = get_connection(connection_uri)

    def _execute(self, query, **params):
        ''' Runs a cypher query returning only raw rows of data.

        Args:
            query: A parameterized cypher query.
            params: The parameters used by the query.

        Returns:
            A generator with the raw rows returned by the connection.
        '''

        rows, _ = cypher.execute(self._conn, query, params)
        for row in rows:
            yield row

    def _convert_value(self, value):
        ''' Converts a py2neo primitive(Node, Relationship, basic object)
        to an equvalent python object.
        Any value which cannot be converted, will be returned as is.

        Args:
            value: The value to convert.

        Returns:
            The converted value.
        '''
        if isinstance(value, (neo4j.Node, neo4j.Relationship)):
            properties = value.get_properties()
            obj = dict_to_object(properties)

            if isinstance(value, neo4j.Relationship):
                obj.start = self._convert_value(value.start_node)
                obj.end = self._convert_value(value.end_node)

            return obj
        return value

    def _convert_row(self, row):
        for value in row:
            yield self._convert_value(value)

    def get(self, cls, **index_filter):
        index_filter = encode_query_values(index_filter)
        descriptor = get_descriptor(cls)

        # MJB: can we consider a different signature that avoids this assert?
        # MJB: something like:
        # MJB: def get(self, cls, (key, value)):
        assert len(index_filter) == 1, "only one index allowed at a time"
        key, value = index_filter.items()[0]

        index_name = descriptor.get_index_name_for_attribute(key)
        node = self._conn.get_indexed_node(index_name, key, value)
        if node is None:
            return None

        obj = self._convert_value(node)
        return obj

    def query(self, query, **params):
        ''' Queries the store given a parameterized cypher query.

        Args:
            query: A parameterized cypher query.
            params: query: A parameterized cypher query.

        Returns:
            A generator with tuples containing stored objects or values.
        '''
        params = encode_query_values(params)
        for row in self._execute(query, **params):
            yield tuple(self._convert_row(row))

    def add(self, obj):
        ''' Adds an object to the data store.

        It will automatically generate the type relationships
        for the the object as required and store the object itself.

        Args:
            obj: The object to store.
        '''

        if not is_persistable(obj):
            raise TypeError('cannot persist %s' % obj)

        if isinstance(obj, Relationship):
            rel_props = object_to_dict(obj)
            query = 'START %s, %s CREATE n1 -[r:%s {rel_props}]-> n2 RETURN r'
            query = query % (
                get_index_query(obj.start, 'n1'),
                get_index_query(obj.end, 'n2'),
                rel_props['__type__'].upper(),
            )
            first(self._execute(query, rel_props=rel_props))
            return

        if obj is PersistableType:
            # create the PersistableType node.
            # if we had a standard start node, we would not need this
            query = 'CREATE (n {props}) RETURN n'
            query_args = {'props': object_to_dict(PersistableType)}
            objects = [PersistableType]
        else:
            try:
                # we have to make sure we have a starting point for
                # the type hierarchy, for now that is PersistableType
                assert self.get(PersistableType, name='PersistableType')
            except:
                self.add(PersistableType)

            query, objects, keys = get_create_types_query(obj)

            query_args = {
                'InstanceOf_props': object_to_dict(InstanceOf(None, None)),
                'IsA_props': object_to_dict(IsA(None, None))
            }

            for key, obj in zip(keys, objects):
                query_args['%s_props' % key] = object_to_dict(obj)

        nodes = first(self._execute(query, **query_args))

        # index all the created nodes
        # infact, we are indexing all nodes, created or not ;-(
        for node, obj in zip(nodes, objects):
            indexes = get_indexes(obj)
            for index_name, key, value in indexes:
                index = self._conn.get_or_create_index(neo4j.Node, index_name)
                index.add(key, value, node)


def is_persistable_not_rel(obj):
    if isinstance(obj, Relationship):
        return False
    elif isinstance(obj, type) and issubclass(obj, Relationship):
        return False
    else:
        return is_persistable(obj)


def is_persistable(obj):
    # MJB: Needs doctring. Objects are persistable iff..
    return (
        isinstance(obj, (Persistable, PersistableType)) or
        (isinstance(obj, type) and issubclass(obj, PersistableType))
        or issubclass(type(type(obj)), PersistableType)
    )

