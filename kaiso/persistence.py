from logging import getLogger

from py2neo import cypher, neo4j

from kaiso.attributes import Outgoing, Incoming, String, Uuid
from kaiso.connection import get_connection
from kaiso.exceptions import UniqueConstraintError
from kaiso.references import set_store_for_object
from kaiso.relationships import InstanceOf
from kaiso.serialize import (
    dict_to_db_values_dict, dict_to_object, object_to_dict, get_changes)
from kaiso.types import (
    Persistable, PersistableMeta, Relationship, AttributedBase,
    get_indexes, get_index_name, is_indexable)
from kaiso.queries import (
    get_create_types_query, get_create_relationship_query, get_start_clause,
    join_lines)

log = getLogger(__name__)


class TypeSystem(AttributedBase):
    id = String(unique=True)
    version = Uuid()


def get_index_filter(obj):
    indexes = get_indexes(obj)
    index_filter = {key: value for _, key, value in indexes}
    return index_filter


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
        log.debug('running query %s', query)

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
            cls, self.type_system, self.dynamic_type)

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
            query = get_create_relationship_query(obj, self.type_system)

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
                'rel_props': object_to_dict(
                    InstanceOf(None, None), self.dynamic_type),
            }

        query_args['props'] = object_to_dict(obj, self.dynamic_type)

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

        existing_props = object_to_dict(existing, self.dynamic_type)
        props = object_to_dict(persistable, self.dynamic_type)

        if existing_props == props:
            # no changes
            return existing

        changes = get_changes(old=existing_props, new=props)
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
