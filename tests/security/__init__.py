# SPDX-License-Identifier: Apache-2.0
"""Security regression tests from adversarial reviews.

Each confirmed finding is a test that asserts the secure behaviour and is marked
``expectedFailure`` while it is open, so fixing it turns the test into an unexpected success
(remove the marker then). Reports: plan/results/security/."""
