"""
Detection summary viewer package.

This package implements the DetectionSummaryViewer AppDaemon app which manages
the run picker, selected run display, viewer cache staging, and notification action
handling for a detection_summary bundle.

The viewer is fully decoupled from detection_summary_app: it communicates via
HA events (``detection_summary/run_published``), shared filesystem, and the
detection_summary_store.
"""
