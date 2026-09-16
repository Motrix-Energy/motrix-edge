"""Both metaclasses guarantee one instance per class, including under contention.

The two share every property that matters, so the tests are parametrized over the
metaclass rather than written twice — the earlier duplicate pair had already drifted
apart in name while staying identical in body.
"""
import threading
from abc import abstractmethod

import pytest

from __metaclasses.singleton import Singleton, AbstractSingleton

METACLASSES = pytest.mark.parametrize("metaclass", [Singleton, AbstractSingleton],
                                      ids=["singleton", "abstract-singleton"])


def make_singleton_class(metaclass):
    """A concrete, instantiable class under `metaclass`.

    AbstractSingleton only guards a *concrete* subclass, so its case needs the abstract
    base plus an implementation; Singleton's case is the class itself.
    """
    if metaclass is AbstractSingleton:
        class Base(metaclass=AbstractSingleton):
            @abstractmethod
            def do(self):
                pass

        class Impl(Base):
            def do(self):
                return 42

        return Impl

    class MyClass(metaclass=metaclass):
        def do(self):
            return 42

    return MyClass


@METACLASSES
def test_returns_same_instance(metaclass):
    cls = make_singleton_class(metaclass)
    a, b = cls(), cls()
    assert a is b
    assert a.do() == 42


@METACLASSES
def test_concurrent_returns_same_instance(metaclass):
    """Ten threads released together: the double-checked lock must admit exactly one."""
    cls = make_singleton_class(metaclass)
    instances = []
    barrier = threading.Barrier(10)

    def create():
        barrier.wait()
        instances.append(cls())

    threads = [threading.Thread(target=create) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(instances) == 10
    assert all(inst is instances[0] for inst in instances)


def test_different_classes_independent():
    class A(metaclass=Singleton):
        pass

    class B(metaclass=Singleton):
        pass

    a, b = A(), B()
    assert a is not b
    assert type(a) is A
    assert type(b) is B
