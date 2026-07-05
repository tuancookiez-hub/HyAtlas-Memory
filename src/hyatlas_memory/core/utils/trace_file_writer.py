# -*- coding: utf-8 -*-
"""向后兼容：请使用 pipeline_log_writer.PipelineLogWriter。"""

from .pipeline_log_writer import PipelineLogWriter, TraceFileWriter

__all__ = ["PipelineLogWriter", "TraceFileWriter"]
