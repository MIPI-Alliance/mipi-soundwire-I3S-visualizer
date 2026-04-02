"""
Property descriptors for validated attributes.

This module provides descriptor classes that reduce boilerplate in property
definitions while maintaining type safety and range validation.
"""

from typing import Any, Optional, Type, TypeVar, Generic, overload, Union


T = TypeVar('T')


class ValidatedInt:
    """Descriptor for validated integer properties with range checking.

    This descriptor reduces boilerplate for properties that need:
    - Type checking (must be int)
    - Range validation (min <= value <= max)
    - Custom error messages

    Example usage:
        class MyClass:
            MIN_VALUE = 0
            MAX_VALUE = 100

            value: int = ValidatedInt('value', MIN_VALUE, MAX_VALUE)  # type: ignore[assignment]

            def __init__(self):
                self.value = 50  # Uses the descriptor
    """

    def __init__(self, name: str, min_val: int, max_val: int,
                 friendly_name: Optional[str] = None) -> None:
        """Initialize the descriptor.

        Args:
            name: The attribute name (used for storage as _name)
            min_val: Minimum allowed value (inclusive)
            max_val: Maximum allowed value (inclusive)
            friendly_name: Human-readable name for error messages (defaults to name)
        """
        self.name = name
        self.private_name = f'_{name}'
        self.min_val = min_val
        self.max_val = max_val
        self.friendly_name = friendly_name or name

    @overload
    def __get__(self, obj: None, objtype: Type[Any]) -> 'ValidatedInt': ...
    @overload
    def __get__(self, obj: object, objtype: Optional[Type[Any]] = None) -> int: ...

    def __get__(self, obj: Optional[object], objtype: Optional[Type[Any]] = None) -> Union['ValidatedInt', int]:
        """Get the attribute value.

        Args:
            obj: The instance to get the attribute from
            objtype: The class type (unused)

        Returns:
            The attribute value, or the descriptor itself if accessed on the class
        """
        if obj is None:
            return self
        return getattr(obj, self.private_name)  # type: ignore[no-any-return]

    def __set__(self, obj: object, value: int) -> None:
        """Set the attribute value with validation.

        Args:
            obj: The instance to set the attribute on
            value: The value to set

        Raises:
            TypeError: If value is not an int
            ValueError: If value is outside the allowed range
        """
        if not isinstance(value, int):
            raise TypeError(f'{self.friendly_name} must be int, got {type(value).__name__}')
        if value > self.max_val:
            raise ValueError(f'{self.friendly_name} must be <= {self.max_val}')
        if value < self.min_val:
            raise ValueError(f'{self.friendly_name} must be >= {self.min_val}')
        setattr(obj, self.private_name, value)


class ValidatedBool:
    """Descriptor for validated boolean properties.

    Example usage:
        class MyClass:
            enabled: bool = ValidatedBool('enabled')  # type: ignore[assignment]

            def __init__(self):
                self.enabled = True  # Uses the descriptor
    """

    def __init__(self, name: str, friendly_name: Optional[str] = None) -> None:
        """Initialize the descriptor.

        Args:
            name: The attribute name (used for storage as _name)
            friendly_name: Human-readable name for error messages (defaults to name)
        """
        self.name = name
        self.private_name = f'_{name}'
        self.friendly_name = friendly_name or name

    @overload
    def __get__(self, obj: None, objtype: Type[Any]) -> 'ValidatedBool': ...
    @overload
    def __get__(self, obj: object, objtype: Optional[Type[Any]] = None) -> bool: ...

    def __get__(self, obj: Optional[object], objtype: Optional[Type[Any]] = None) -> Union['ValidatedBool', bool]:
        """Get the attribute value."""
        if obj is None:
            return self
        return getattr(obj, self.private_name)  # type: ignore[no-any-return]

    def __set__(self, obj: object, value: Any) -> None:
        """Set the attribute value with validation.

        Args:
            obj: The instance to set the attribute on
            value: The value to set (accepts bool, int, or string "True"/"False")

        Raises:
            TypeError: If value cannot be converted to bool
        """
        # Convert int to bool (0 = False, non-zero = True)
        if isinstance(value, int) and not isinstance(value, bool):
            value = bool(value)
        # Convert string to bool
        elif isinstance(value, str):
            if value.lower() in ('true', '1'):
                value = True
            elif value.lower() in ('false', '0'):
                value = False
            else:
                raise TypeError(f'{self.friendly_name} must be bool, got string "{value}"')
        elif not isinstance(value, bool):
            raise TypeError(f'{self.friendly_name} must be bool, got {type(value).__name__}')
        setattr(obj, self.private_name, value)


class ValidatedString:
    """Descriptor for validated string properties.

    Example usage:
        class MyClass:
            name: str = ValidatedString('name')  # type: ignore[assignment]

            def __init__(self):
                self.name = "test"  # Uses the descriptor
    """

    def __init__(self, name: str, friendly_name: Optional[str] = None) -> None:
        """Initialize the descriptor.

        Args:
            name: The attribute name (used for storage as _name)
            friendly_name: Human-readable name for error messages (defaults to name)
        """
        self.name = name
        self.private_name = f'_{name}'
        self.friendly_name = friendly_name or name

    @overload
    def __get__(self, obj: None, objtype: Type[Any]) -> 'ValidatedString': ...
    @overload
    def __get__(self, obj: object, objtype: Optional[Type[Any]] = None) -> str: ...

    def __get__(self, obj: Optional[object], objtype: Optional[Type[Any]] = None) -> Union['ValidatedString', str]:
        """Get the attribute value."""
        if obj is None:
            return self
        return getattr(obj, self.private_name)  # type: ignore[no-any-return]

    def __set__(self, obj: object, value: str) -> None:
        """Set the attribute value with validation.

        Args:
            obj: The instance to set the attribute on
            value: The value to set

        Raises:
            TypeError: If value is not a string
        """
        if not isinstance(value, str):
            raise TypeError(f'{self.friendly_name} must be a string, got {type(value).__name__}')
        setattr(obj, self.private_name, value)


class ValidatedIntWithCallback(ValidatedInt):
    """Descriptor for validated integer properties with a post-set callback.

    Useful when setting a property needs to trigger side effects, like
    invalidating a cache.

    Example usage:
        class MyClass:
            MIN_VALUE = 0
            MAX_VALUE = 0xFFFF

            def _invalidate_cache(self):
                self._cache = None

            bitmask: int = ValidatedIntWithCallback(  # type: ignore[assignment]
                'bitmask', MIN_VALUE, MAX_VALUE,
                on_set='_invalidate_cache'
            )
    """

    def __init__(self, name: str, min_val: int, max_val: int,
                 friendly_name: Optional[str] = None,
                 on_set: Optional[str] = None) -> None:
        """Initialize the descriptor with optional callback.

        Args:
            name: The attribute name (used for storage as _name)
            min_val: Minimum allowed value (inclusive)
            max_val: Maximum allowed value (inclusive)
            friendly_name: Human-readable name for error messages (defaults to name)
            on_set: Name of method to call after setting value (optional)
        """
        super().__init__(name, min_val, max_val, friendly_name)
        self.on_set = on_set

    def __set__(self, obj: object, value: int) -> None:
        """Set the attribute value with validation and callback.

        Args:
            obj: The instance to set the attribute on
            value: The value to set

        Raises:
            TypeError: If value is not an int
            ValueError: If value is outside the allowed range
        """
        super().__set__(obj, value)
        if self.on_set:
            callback = getattr(obj, self.on_set, None)
            if callback:
                callback()
