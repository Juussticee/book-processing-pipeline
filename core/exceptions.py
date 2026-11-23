"""Custom exceptions for Book Processor."""


class BookProcessorError(Exception):
    """Base exception for Book Processor."""


class ConfigurationError(BookProcessorError):
    """Raised when configuration is invalid or missing."""


class ProcessingError(BookProcessorError):
    """Raised when a processing step fails."""
