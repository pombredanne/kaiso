import pytest

from kaiso.exceptions import DeserialisationError, UnknownType
from kaiso.attributes import String
from kaiso.relationships import Relationship, InstanceOf, IsA
from kaiso.serialize import (
    get_type_relationships, object_to_dict, dict_to_object)
from kaiso.types import (
    Persistable, MetaMeta, PersistableMeta, Entity, AttributedBase, Attribute)


class Foo(Entity):
    pass


class Bar(Attribute):
    pass


def test_classes_to_dict():
    dct = object_to_dict(Entity)
    assert dct == {'__type__': 'PersistableMeta', 'id': 'Entity'}

    obj = dict_to_object(dct)
    assert obj is Entity

    dct = object_to_dict(Foo)
    assert dct == {'__type__': 'PersistableMeta', 'id': 'Foo'}

    obj = dict_to_object(dct)
    assert obj is Foo


def test_objects():
    dct = object_to_dict(Entity())
    assert dct == {'__type__': 'Entity'}

    obj = dict_to_object(dct)
    assert isinstance(obj, Entity)

    dct = object_to_dict(Foo())
    assert dct == {'__type__': 'Foo'}

    obj = dict_to_object(dct)
    assert isinstance(obj, Foo)


def test_attribute():
    attr = Bar(unique=True)
    dct = object_to_dict(attr)
    assert dct == {'__type__': 'Bar', 'unique': True}

    obj = dict_to_object({'__type__': 'Bar', 'unique': True})
    assert isinstance(obj, Bar)
    assert obj.unique is True


def test_relationship():
    dct = object_to_dict(Relationship(None, None))
    assert dct == {'__type__': 'Relationship'}

    obj = dict_to_object(dct)
    assert isinstance(obj, Relationship)


def test_dynamic_type():
    DynamicType = type(PersistableMeta.__name__, (PersistableMeta,), {})

    Foobar = DynamicType('Foobar', (Entity,), {})

    dct = object_to_dict(Foobar)
    assert dct == {'__type__': 'PersistableMeta', 'id': 'Foobar'}

    # since Foobar is only registered with DynamicType
    with pytest.raises(UnknownType):
        obj = dict_to_object(dct)

    obj = dict_to_object(dct, DynamicType)
    assert obj is Foobar


def test_dynamic_typed_object():
    DynamicType = type(PersistableMeta.__name__, (PersistableMeta,), {})

    attrs = {'id': String()}
    Foobar = DynamicType('Foobar', (Entity,), attrs)

    foo = Foobar(id='spam')
    dct = object_to_dict(foo)

    assert dct == {'__type__': 'Foobar', 'id': 'spam'}

    obj = dict_to_object(dct, DynamicType)
    assert isinstance(obj, Foobar)
    assert obj.id == foo.id


def test_dynamic_typed_object_same_type():
    DynamicType = type(PersistableMeta.__name__, (PersistableMeta,), {})

    attrs = {'id': String()}
    DynEntity = DynamicType('Entity', (AttributedBase,), attrs)

    foo = DynEntity(id='spam')
    dct = object_to_dict(foo)

    assert dct == {'__type__': 'Entity', 'id': 'spam'}

    obj = dict_to_object(dct, DynamicType)
    assert isinstance(obj, Entity)
    assert obj.id == foo.id


def test_missing_info():
    with pytest.raises(DeserialisationError):
        dict_to_object({})


def test_IsA_and_InstanceOf_type_relationships():
    """ Testing type hierarchy creation.

    We don't want to test all the types in their relationships,
    but only the set of types which are persisted with IsA and InstanceOf
    relationships.
    """
    result = list(get_type_relationships(Entity))

    assert result == [
        (object, InstanceOf, type),
        (type, IsA, object),
        (type, InstanceOf, type),
        (MetaMeta, IsA, type),
        (MetaMeta, InstanceOf, type),
        (PersistableMeta, IsA, type),
        (Persistable, IsA, object),
        (Persistable, InstanceOf, type),
        (PersistableMeta, IsA, Persistable),
        (PersistableMeta, InstanceOf, MetaMeta),
        (AttributedBase, IsA, Persistable),
        (AttributedBase, InstanceOf, PersistableMeta),
        (Entity, IsA, AttributedBase),
        (Entity, InstanceOf, PersistableMeta),
    ]

    pers = Entity()
    result = list(get_type_relationships(pers))

    assert result == [
        (object, InstanceOf, type),
        (type, IsA, object),
        (type, InstanceOf, type),
        (MetaMeta, IsA, type),
        (MetaMeta, InstanceOf, type),
        (PersistableMeta, IsA, type),
        (Persistable, IsA, object),
        (Persistable, InstanceOf, type),
        (PersistableMeta, IsA, Persistable),
        (PersistableMeta, InstanceOf, MetaMeta),
        (AttributedBase, IsA, Persistable),
        (AttributedBase, InstanceOf, PersistableMeta),
        (Entity, IsA, AttributedBase),
        (Entity, InstanceOf, PersistableMeta),
        (pers, InstanceOf, Entity),
    ]

    foo = Foo()
    result = list(get_type_relationships(foo))

    assert result == [
        (object, InstanceOf, type),
        (type, IsA, object),
        (type, InstanceOf, type),
        (MetaMeta, IsA, type),
        (MetaMeta, InstanceOf, type),
        (PersistableMeta, IsA, type),
        (Persistable, IsA, object),
        (Persistable, InstanceOf, type),
        (PersistableMeta, IsA, Persistable),
        (PersistableMeta, InstanceOf, MetaMeta),
        (AttributedBase, IsA, Persistable),
        (AttributedBase, InstanceOf, PersistableMeta),
        (Entity, IsA, AttributedBase),
        (Entity, InstanceOf, PersistableMeta),
        (Foo, IsA, Entity),
        (Foo, InstanceOf, PersistableMeta),
        (foo, InstanceOf, Foo),
    ]
