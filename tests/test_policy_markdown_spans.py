"""Conservative Markdown/YAML span mapping for fragment policy labels."""

import os
import tempfile

import anfs_core


def test_markdown_frontmatter_policy_labels_map_semantic_fields_to_fragments():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = (
            b"---\n"
            b"title: Contract note\n"
            b"owner_email: ada@example.com\n"
            b"\"owner.email\": quoted@example.com\n"
            b"owner:\n"
            b"  email: nested@example.com\n"
            b"  team: privacy\n"
            b"quoted_parent:\n"
            b"  \"team:name\": quoted-team\n"
            b"recipients:\n"
            b"  - legal@example.com\n"
            b"  - name: Ada Lovelace\n"
            b"summary: |\n"
            b"  Confidential line one\n"
            b"  Confidential line two\n"
            b"optional_owner: null\n"
            b"risk_score: 7\n"
            b"effective_date: 2026-06-09\n"
            b"reviewed_at: 2026-06-09T12:30:45Z\n"
            b"quoted_date: \"2026-06-09\"\n"
            b"quoted_scalars: [\"7\", 'true', !!int \"8\"]\n"
            b"escaped_scalar: \"Line\\nTwo \\u263A\"\n"
            b"single_quoted_escape: 'Ada''s team'\n"
            b"escaped_inline: {message: \"said \\\"hi\\\", then left\", team: ops}\n"
            b"escaped_sequence: [\"a, \\\"b\\\"\", \"line\\nnext\", 'Ada''s']\n"
            b"tags: [public, secret, 7, false]\n"
            b"date_values: [2026-06-09, \"2026-06-10\", 2026-06-09 12:30:45+00:00]\n"
            b"missing_values: [null, ~, present]\n"
            b"typed_score: !!str 7\n"
            b"typed_missing: !!null null\n"
            b"typed_when: !!timestamp 2026-06-09T12:00:00Z\n"
            b"typed_blob: !!binary SGVsbG8=\n"
            b"typed_set: !!set {read: null, write: null}\n"
            b"typed_omap: !!omap [{first: one}, {second: two}]\n"
            b"typed_values: [!!str 7, !!int 7, !!float 7.5, !!bool true, !!null null, !<tag:yaml.org,2002:str> 42, !<tag:yaml.org,2002:timestamp> 2026-06-09, !!binary SGVsbG8=]\n"
            b"typed_containers: [!!set {alpha: null}, !<tag:yaml.org,2002:omap> [{beta: two}]]\n"
            b"contact: {email: inline@example.com, team: legal, groups: [legal, privacy], manager: {email: manager@example.com, team: privacy}, priority: 3, active: true}\n"
            b"quoted_contact: {\"email.addr\": quoted-inline@example.com, 'team name': ops}\n"
            b"contacts: [{email: first@example.com, team: legal, meta: {risk: high, flags: [urgent, restricted], owner: Ada}}, {\"email.addr\": second@example.com, team: ops}]\n"
            b"decorated_owner: !pii &primary tagged@example.com\n"
            b"decorated_contact: !contact &contact_anchor {email: decorated@example.com, flags: !flagseq [review, !secret decorated-secret]}\n"
            b"alias_owner: *primary\n"
            b"alias_contact: {email: *primary, backups: [*primary, reviewer]}\n"
            b"anchored_template: &contact_template {email: template@example.com, flags: [sensitive-template, public-template]}\n"
            b"phone_template: &phone_template {phone: 555-0100}\n"
            b"alias_template: *contact_template\n"
            b"alias_template_chain: &template_alias *contact_template\n"
            b"alias_template_deep: *template_alias\n"
            b"merged_template: {<<: *contact_template, team: merged-team}\n"
            b"merged_template_chain: {<<: *template_alias, team: chain-team}\n"
            b"merged_array_template: {<<: [*contact_template, *phone_template], team: array-team}\n"
            b"scalar_alias_chain: &secondary *primary\n"
            b"scalar_alias_deep: *secondary\n"
            b"block_aliases:\n"
            b"  - *primary\n"
            b"  - reviewer\n"
            b"block_alias_field:\n"
            b"  - email: *primary\n"
            b"block_merge_items:\n"
            b"  - &block_template {email: block-template@example.com, label: inherited}\n"
            b"  - {<<: *block_template, label: explicit}\n"
            b"---\n"
            b"Body mentions renewal.\n"
        )
        node_id = ws.write("note.md", content, [])
        ws.publish("note.md", "artifact:note@v1")

        spans = fs.markdown_field_spans(node_id)
        span_by_path = {row[0]: row for row in spans}
        values_by_path = {row[0]: row for row in fs.markdown_field_values(node_id)}
        email_path, email_offset, email_length, email_kind = span_by_path[
            "frontmatter.owner_email"
        ]
        assert (email_path, email_kind) == ("frontmatter.owner_email", "string")
        assert content[email_offset : email_offset + email_length] == b"ada@example.com"
        nested_path, nested_offset, nested_length, nested_kind = span_by_path[
            "frontmatter.owner.email"
        ]
        assert (nested_path, nested_kind) == ("frontmatter.owner.email", "string")
        assert content[nested_offset : nested_offset + nested_length] == b"nested@example.com"
        quoted_top = span_by_path['frontmatter["owner.email"]']
        assert quoted_top[3] == "string"
        assert content[quoted_top[1] : quoted_top[1] + quoted_top[2]] == b"quoted@example.com"
        assert span_by_path["frontmatter.owner.team"][3] == "string"
        quoted_nested = span_by_path['frontmatter.quoted_parent["team:name"]']
        assert quoted_nested[3] == "string"
        assert content[quoted_nested[1] : quoted_nested[1] + quoted_nested[2]] == b"quoted-team"
        owner_span = span_by_path["frontmatter.owner"]
        assert owner_span[3] == "object"
        assert content[owner_span[1] : owner_span[1] + owner_span[2]].startswith(b"owner:\n")
        recipients_span = span_by_path["frontmatter.recipients"]
        assert recipients_span[3] == "array"
        assert b"legal@example.com" in content[
            recipients_span[1] : recipients_span[1] + recipients_span[2]
        ]
        first_recipient = span_by_path["frontmatter.recipients[0]"]
        assert first_recipient[3] == "string"
        assert content[first_recipient[1] : first_recipient[1] + first_recipient[2]] == (
            b"legal@example.com"
        )
        recipient_name = span_by_path["frontmatter.recipients[1].name"]
        assert recipient_name[3] == "string"
        assert content[recipient_name[1] : recipient_name[1] + recipient_name[2]] == (
            b"Ada Lovelace"
        )
        summary_span = span_by_path["frontmatter.summary"]
        assert summary_span[3] == "string"
        assert b"Confidential line two" in content[
            summary_span[1] : summary_span[1] + summary_span[2]
        ]
        optional_owner = span_by_path["frontmatter.optional_owner"]
        assert optional_owner[3] == "null"
        assert content[
            optional_owner[1] : optional_owner[1] + optional_owner[2]
        ] == b"null"
        assert span_by_path["frontmatter.risk_score"][3] == "number"
        effective_date = span_by_path["frontmatter.effective_date"]
        reviewed_at = span_by_path["frontmatter.reviewed_at"]
        quoted_date = span_by_path["frontmatter.quoted_date"]
        assert effective_date[3] == "timestamp"
        assert reviewed_at[3] == "timestamp"
        assert quoted_date[3] == "string"
        assert content[effective_date[1] : effective_date[1] + effective_date[2]] == (
            b"2026-06-09"
        )
        assert content[reviewed_at[1] : reviewed_at[1] + reviewed_at[2]] == (
            b"2026-06-09T12:30:45Z"
        )
        assert content[quoted_date[1] : quoted_date[1] + quoted_date[2]] == (
            b"2026-06-09"
        )
        quoted_scalars = span_by_path["frontmatter.quoted_scalars"]
        quoted_number = span_by_path["frontmatter.quoted_scalars[0]"]
        quoted_bool = span_by_path["frontmatter.quoted_scalars[1]"]
        typed_quoted_int = span_by_path["frontmatter.quoted_scalars[2]"]
        assert quoted_scalars[3] == "array"
        assert quoted_number[3] == quoted_bool[3] == "string"
        assert typed_quoted_int[3] == "number"
        assert content[quoted_number[1] : quoted_number[1] + quoted_number[2]] == b"7"
        assert content[quoted_bool[1] : quoted_bool[1] + quoted_bool[2]] == b"true"
        assert content[
            typed_quoted_int[1] : typed_quoted_int[1] + typed_quoted_int[2]
        ] == b"8"
        escaped_scalar = span_by_path["frontmatter.escaped_scalar"]
        single_quoted_escape = span_by_path["frontmatter.single_quoted_escape"]
        escaped_inline = span_by_path["frontmatter.escaped_inline"]
        escaped_inline_message = span_by_path["frontmatter.escaped_inline.message"]
        escaped_sequence = span_by_path["frontmatter.escaped_sequence"]
        escaped_sequence_first = span_by_path["frontmatter.escaped_sequence[0]"]
        escaped_sequence_second = span_by_path["frontmatter.escaped_sequence[1]"]
        escaped_sequence_third = span_by_path["frontmatter.escaped_sequence[2]"]
        assert escaped_scalar[3] == single_quoted_escape[3] == "string"
        assert escaped_inline[3] == "object"
        assert escaped_sequence[3] == "array"
        assert escaped_inline_message[3] == "string"
        assert escaped_sequence_first[3] == escaped_sequence_second[3] == "string"
        assert escaped_sequence_third[3] == "string"
        assert content[
            escaped_scalar[1] : escaped_scalar[1] + escaped_scalar[2]
        ] == b"Line\\nTwo \\u263A"
        assert content[
            escaped_inline_message[1] : escaped_inline_message[1]
            + escaped_inline_message[2]
        ] == b"said \\\"hi\\\", then left"
        assert values_by_path["frontmatter.escaped_scalar"] == (
            "frontmatter.escaped_scalar",
            "Line\nTwo \u263A",
            "string",
        )
        assert values_by_path["frontmatter.single_quoted_escape"] == (
            "frontmatter.single_quoted_escape",
            "Ada's team",
            "string",
        )
        assert values_by_path["frontmatter.escaped_inline.message"] == (
            "frontmatter.escaped_inline.message",
            'said "hi", then left',
            "string",
        )
        assert values_by_path["frontmatter.escaped_sequence[0]"] == (
            "frontmatter.escaped_sequence[0]",
            'a, "b"',
            "string",
        )
        assert values_by_path["frontmatter.escaped_sequence[1]"] == (
            "frontmatter.escaped_sequence[1]",
            "line\nnext",
            "string",
        )
        assert values_by_path["frontmatter.escaped_sequence[2]"] == (
            "frontmatter.escaped_sequence[2]",
            "Ada's",
            "string",
        )
        tags_span = span_by_path["frontmatter.tags"]
        assert tags_span[3] == "array"
        assert content[tags_span[1] : tags_span[1] + tags_span[2]] == (
            b"[public, secret, 7, false]"
        )
        first_tag = span_by_path["frontmatter.tags[0]"]
        secret_tag = span_by_path["frontmatter.tags[1]"]
        numeric_tag = span_by_path["frontmatter.tags[2]"]
        bool_tag = span_by_path["frontmatter.tags[3]"]
        assert first_tag[3] == secret_tag[3] == "string"
        assert numeric_tag[3] == "number"
        assert bool_tag[3] == "bool"
        assert content[first_tag[1] : first_tag[1] + first_tag[2]] == b"public"
        assert content[secret_tag[1] : secret_tag[1] + secret_tag[2]] == b"secret"
        date_values = span_by_path["frontmatter.date_values"]
        date_value = span_by_path["frontmatter.date_values[0]"]
        quoted_date_value = span_by_path["frontmatter.date_values[1]"]
        datetime_value = span_by_path["frontmatter.date_values[2]"]
        assert date_values[3] == "array"
        assert date_value[3] == "timestamp"
        assert quoted_date_value[3] == "string"
        assert datetime_value[3] == "timestamp"
        assert content[date_value[1] : date_value[1] + date_value[2]] == b"2026-06-09"
        assert content[
            quoted_date_value[1] : quoted_date_value[1] + quoted_date_value[2]
        ] == b"2026-06-10"
        assert content[datetime_value[1] : datetime_value[1] + datetime_value[2]] == (
            b"2026-06-09 12:30:45+00:00"
        )
        missing_values = span_by_path["frontmatter.missing_values"]
        missing_null = span_by_path["frontmatter.missing_values[0]"]
        missing_tilde = span_by_path["frontmatter.missing_values[1]"]
        missing_present = span_by_path["frontmatter.missing_values[2]"]
        assert missing_values[3] == "array"
        assert missing_null[3] == missing_tilde[3] == "null"
        assert missing_present[3] == "string"
        assert content[missing_null[1] : missing_null[1] + missing_null[2]] == b"null"
        assert content[missing_tilde[1] : missing_tilde[1] + missing_tilde[2]] == b"~"
        typed_score = span_by_path["frontmatter.typed_score"]
        typed_missing = span_by_path["frontmatter.typed_missing"]
        typed_when = span_by_path["frontmatter.typed_when"]
        typed_blob = span_by_path["frontmatter.typed_blob"]
        typed_set = span_by_path["frontmatter.typed_set"]
        typed_set_read = span_by_path["frontmatter.typed_set.read"]
        typed_omap = span_by_path["frontmatter.typed_omap"]
        typed_omap_first = span_by_path["frontmatter.typed_omap[0].first"]
        typed_values = span_by_path["frontmatter.typed_values"]
        typed_str = span_by_path["frontmatter.typed_values[0]"]
        typed_int = span_by_path["frontmatter.typed_values[1]"]
        typed_float = span_by_path["frontmatter.typed_values[2]"]
        typed_bool = span_by_path["frontmatter.typed_values[3]"]
        typed_null = span_by_path["frontmatter.typed_values[4]"]
        typed_uri_str = span_by_path["frontmatter.typed_values[5]"]
        typed_uri_timestamp = span_by_path["frontmatter.typed_values[6]"]
        typed_binary_item = span_by_path["frontmatter.typed_values[7]"]
        typed_containers = span_by_path["frontmatter.typed_containers"]
        typed_container_set = span_by_path["frontmatter.typed_containers[0]"]
        typed_container_omap = span_by_path["frontmatter.typed_containers[1]"]
        assert typed_score[3] == "string"
        assert typed_missing[3] == "null"
        assert typed_when[3] == "timestamp"
        assert typed_blob[3] == "binary"
        assert typed_set[3] == "set"
        assert typed_set_read[3] == "null"
        assert typed_omap[3] == "omap"
        assert typed_omap_first[3] == "string"
        assert typed_values[3] == "array"
        assert typed_str[3] == "string"
        assert typed_int[3] == typed_float[3] == "number"
        assert typed_bool[3] == "bool"
        assert typed_null[3] == "null"
        assert typed_uri_str[3] == "string"
        assert typed_uri_timestamp[3] == "timestamp"
        assert typed_binary_item[3] == "binary"
        assert typed_containers[3] == "array"
        assert typed_container_set[3] == "set"
        assert typed_container_omap[3] == "omap"
        assert content[typed_score[1] : typed_score[1] + typed_score[2]] == b"7"
        assert content[
            typed_missing[1] : typed_missing[1] + typed_missing[2]
        ] == b"null"
        assert content[
            typed_when[1] : typed_when[1] + typed_when[2]
        ] == b"2026-06-09T12:00:00Z"
        assert content[typed_blob[1] : typed_blob[1] + typed_blob[2]] == b"SGVsbG8="
        assert content[typed_set[1] : typed_set[1] + typed_set[2]] == (
            b"{read: null, write: null}"
        )
        assert content[typed_set_read[1] : typed_set_read[1] + typed_set_read[2]] == b"null"
        assert content[typed_omap[1] : typed_omap[1] + typed_omap[2]] == (
            b"[{first: one}, {second: two}]"
        )
        assert content[typed_omap_first[1] : typed_omap_first[1] + typed_omap_first[2]] == b"one"
        assert content[typed_str[1] : typed_str[1] + typed_str[2]] == b"7"
        assert content[typed_int[1] : typed_int[1] + typed_int[2]] == b"7"
        assert content[typed_float[1] : typed_float[1] + typed_float[2]] == b"7.5"
        assert content[typed_bool[1] : typed_bool[1] + typed_bool[2]] == b"true"
        assert content[typed_null[1] : typed_null[1] + typed_null[2]] == b"null"
        assert content[typed_uri_str[1] : typed_uri_str[1] + typed_uri_str[2]] == b"42"
        assert content[
            typed_uri_timestamp[1] : typed_uri_timestamp[1] + typed_uri_timestamp[2]
        ] == b"2026-06-09"
        assert content[
            typed_binary_item[1] : typed_binary_item[1] + typed_binary_item[2]
        ] == b"SGVsbG8="
        assert content[
            typed_container_set[1] : typed_container_set[1] + typed_container_set[2]
        ] == b"{alpha: null}"
        assert content[
            typed_container_omap[1] : typed_container_omap[1] + typed_container_omap[2]
        ] == b"[{beta: two}]"
        contact_span = span_by_path["frontmatter.contact"]
        contact_email = span_by_path["frontmatter.contact.email"]
        contact_team = span_by_path["frontmatter.contact.team"]
        contact_groups = span_by_path["frontmatter.contact.groups"]
        contact_group_legal = span_by_path["frontmatter.contact.groups[0]"]
        contact_group_privacy = span_by_path["frontmatter.contact.groups[1]"]
        contact_manager = span_by_path["frontmatter.contact.manager"]
        contact_manager_email = span_by_path["frontmatter.contact.manager.email"]
        contact_manager_team = span_by_path["frontmatter.contact.manager.team"]
        contact_priority = span_by_path["frontmatter.contact.priority"]
        contact_active = span_by_path["frontmatter.contact.active"]
        assert contact_span[3] == "object"
        assert content[contact_span[1] : contact_span[1] + contact_span[2]].startswith(
            b"{email: inline@example.com"
        )
        assert contact_email[3] == contact_team[3] == "string"
        assert contact_groups[3] == "array"
        assert contact_group_legal[3] == contact_group_privacy[3] == "string"
        assert content[
            contact_group_privacy[1] : contact_group_privacy[1] + contact_group_privacy[2]
        ] == b"privacy"
        assert contact_manager[3] == "object"
        assert contact_manager_email[3] == contact_manager_team[3] == "string"
        assert contact_priority[3] == "number"
        assert contact_active[3] == "bool"
        assert content[contact_email[1] : contact_email[1] + contact_email[2]] == (
            b"inline@example.com"
        )
        assert content[
            contact_manager_email[1] : contact_manager_email[1] + contact_manager_email[2]
        ] == b"manager@example.com"
        quoted_contact = span_by_path["frontmatter.quoted_contact"]
        quoted_contact_email = span_by_path['frontmatter.quoted_contact["email.addr"]']
        quoted_contact_team = span_by_path['frontmatter.quoted_contact["team name"]']
        assert quoted_contact[3] == "object"
        assert quoted_contact_email[3] == quoted_contact_team[3] == "string"
        assert content[
            quoted_contact_email[1] : quoted_contact_email[1] + quoted_contact_email[2]
        ] == b"quoted-inline@example.com"
        contacts_span = span_by_path["frontmatter.contacts"]
        first_contact = span_by_path["frontmatter.contacts[0]"]
        first_contact_email = span_by_path["frontmatter.contacts[0].email"]
        first_contact_team = span_by_path["frontmatter.contacts[0].team"]
        first_contact_meta = span_by_path["frontmatter.contacts[0].meta"]
        first_contact_meta_risk = span_by_path["frontmatter.contacts[0].meta.risk"]
        first_contact_meta_flags = span_by_path["frontmatter.contacts[0].meta.flags"]
        first_contact_meta_flag_urgent = span_by_path["frontmatter.contacts[0].meta.flags[0]"]
        first_contact_meta_flag_restricted = span_by_path["frontmatter.contacts[0].meta.flags[1]"]
        first_contact_meta_owner = span_by_path["frontmatter.contacts[0].meta.owner"]
        second_contact = span_by_path["frontmatter.contacts[1]"]
        second_contact_email = span_by_path['frontmatter.contacts[1]["email.addr"]']
        second_contact_team = span_by_path["frontmatter.contacts[1].team"]
        assert contacts_span[3] == "array"
        assert first_contact[3] == second_contact[3] == "object"
        assert first_contact_email[3] == first_contact_team[3] == "string"
        assert first_contact_meta[3] == "object"
        assert first_contact_meta_flags[3] == "array"
        assert first_contact_meta_flag_urgent[3] == first_contact_meta_flag_restricted[3] == "string"
        assert first_contact_meta_risk[3] == first_contact_meta_owner[3] == "string"
        assert second_contact_email[3] == second_contact_team[3] == "string"
        assert content[
            first_contact_email[1] : first_contact_email[1] + first_contact_email[2]
        ] == b"first@example.com"
        assert content[
            first_contact_meta_risk[1] : first_contact_meta_risk[1] + first_contact_meta_risk[2]
        ] == b"high"
        assert content[
            first_contact_meta_flag_restricted[1] : first_contact_meta_flag_restricted[1] + first_contact_meta_flag_restricted[2]
        ] == b"restricted"
        assert content[
            second_contact_email[1] : second_contact_email[1] + second_contact_email[2]
        ] == b"second@example.com"
        decorated_owner = span_by_path["frontmatter.decorated_owner"]
        decorated_contact = span_by_path["frontmatter.decorated_contact"]
        decorated_contact_email = span_by_path["frontmatter.decorated_contact.email"]
        decorated_contact_flags = span_by_path["frontmatter.decorated_contact.flags"]
        decorated_contact_flag_review = span_by_path["frontmatter.decorated_contact.flags[0]"]
        decorated_contact_flag_secret = span_by_path["frontmatter.decorated_contact.flags[1]"]
        assert decorated_owner[3] == "string"
        assert decorated_contact[3] == "object"
        assert decorated_contact_email[3] == "string"
        assert decorated_contact_flags[3] == "array"
        assert decorated_contact_flag_review[3] == decorated_contact_flag_secret[3] == "string"
        assert content[
            decorated_owner[1] : decorated_owner[1] + decorated_owner[2]
        ] == b"tagged@example.com"
        assert content[
            decorated_contact_email[1] : decorated_contact_email[1] + decorated_contact_email[2]
        ] == b"decorated@example.com"
        assert content[
            decorated_contact_flag_secret[1] : decorated_contact_flag_secret[1] + decorated_contact_flag_secret[2]
        ] == b"decorated-secret"
        alias_owner = span_by_path["frontmatter.alias_owner"]
        alias_owner_target = span_by_path["frontmatter.alias_owner.__target"]
        alias_contact = span_by_path["frontmatter.alias_contact"]
        alias_contact_email = span_by_path["frontmatter.alias_contact.email"]
        alias_contact_email_target = span_by_path["frontmatter.alias_contact.email.__target"]
        alias_contact_backups = span_by_path["frontmatter.alias_contact.backups"]
        alias_contact_backup_primary = span_by_path["frontmatter.alias_contact.backups[0]"]
        alias_contact_backup_primary_target = span_by_path[
            "frontmatter.alias_contact.backups[0].__target"
        ]
        alias_contact_backup_reviewer = span_by_path["frontmatter.alias_contact.backups[1]"]
        anchored_template = span_by_path["frontmatter.anchored_template"]
        anchored_template_email = span_by_path["frontmatter.anchored_template.email"]
        anchored_template_flags = span_by_path["frontmatter.anchored_template.flags"]
        anchored_template_flag_secret = span_by_path[
            "frontmatter.anchored_template.flags[0]"
        ]
        phone_template = span_by_path["frontmatter.phone_template"]
        phone_template_phone = span_by_path["frontmatter.phone_template.phone"]
        alias_template = span_by_path["frontmatter.alias_template"]
        alias_template_email = span_by_path["frontmatter.alias_template.email"]
        alias_template_flags = span_by_path["frontmatter.alias_template.flags"]
        alias_template_flag_secret = span_by_path["frontmatter.alias_template.flags[0]"]
        alias_template_chain = span_by_path["frontmatter.alias_template_chain"]
        alias_template_chain_email = span_by_path["frontmatter.alias_template_chain.email"]
        alias_template_chain_flags = span_by_path["frontmatter.alias_template_chain.flags"]
        alias_template_chain_flag_secret = span_by_path[
            "frontmatter.alias_template_chain.flags[0]"
        ]
        alias_template_deep = span_by_path["frontmatter.alias_template_deep"]
        alias_template_deep_email = span_by_path["frontmatter.alias_template_deep.email"]
        alias_template_deep_flags = span_by_path["frontmatter.alias_template_deep.flags"]
        alias_template_deep_flag_secret = span_by_path[
            "frontmatter.alias_template_deep.flags[0]"
        ]
        merged_template = span_by_path["frontmatter.merged_template"]
        merged_template_email = span_by_path["frontmatter.merged_template.email"]
        merged_template_flags = span_by_path["frontmatter.merged_template.flags"]
        merged_template_flag_secret = span_by_path["frontmatter.merged_template.flags[0]"]
        merged_template_team = span_by_path["frontmatter.merged_template.team"]
        merged_template_chain = span_by_path["frontmatter.merged_template_chain"]
        merged_template_chain_email = span_by_path[
            "frontmatter.merged_template_chain.email"
        ]
        merged_template_chain_flags = span_by_path[
            "frontmatter.merged_template_chain.flags"
        ]
        merged_template_chain_flag_secret = span_by_path[
            "frontmatter.merged_template_chain.flags[0]"
        ]
        merged_template_chain_team = span_by_path[
            "frontmatter.merged_template_chain.team"
        ]
        merged_array_template = span_by_path["frontmatter.merged_array_template"]
        merged_array_template_email = span_by_path[
            "frontmatter.merged_array_template.email"
        ]
        merged_array_template_flags = span_by_path[
            "frontmatter.merged_array_template.flags"
        ]
        merged_array_template_phone = span_by_path[
            "frontmatter.merged_array_template.phone"
        ]
        merged_array_template_team = span_by_path["frontmatter.merged_array_template.team"]
        scalar_alias_chain = span_by_path["frontmatter.scalar_alias_chain"]
        scalar_alias_chain_target = span_by_path[
            "frontmatter.scalar_alias_chain.__target"
        ]
        scalar_alias_deep = span_by_path["frontmatter.scalar_alias_deep"]
        scalar_alias_deep_target = span_by_path["frontmatter.scalar_alias_deep.__target"]
        block_aliases = span_by_path["frontmatter.block_aliases"]
        block_alias_primary = span_by_path["frontmatter.block_aliases[0]"]
        block_alias_primary_target = span_by_path["frontmatter.block_aliases[0].__target"]
        block_alias_reviewer = span_by_path["frontmatter.block_aliases[1]"]
        block_alias_field = span_by_path["frontmatter.block_alias_field"]
        block_alias_field_email = span_by_path["frontmatter.block_alias_field[0].email"]
        block_alias_field_email_target = span_by_path[
            "frontmatter.block_alias_field[0].email.__target"
        ]
        block_merge_items = span_by_path["frontmatter.block_merge_items"]
        block_merge_template = span_by_path["frontmatter.block_merge_items[0]"]
        block_merge_template_email = span_by_path[
            "frontmatter.block_merge_items[0].email"
        ]
        block_merge_template_label = span_by_path[
            "frontmatter.block_merge_items[0].label"
        ]
        block_merge_item = span_by_path["frontmatter.block_merge_items[1]"]
        block_merge_item_email = span_by_path["frontmatter.block_merge_items[1].email"]
        block_merge_item_label = span_by_path["frontmatter.block_merge_items[1].label"]
        assert alias_owner[3] == "alias"
        assert alias_owner_target[3] == "string"
        assert alias_contact[3] == "object"
        assert alias_contact_email[3] == "alias"
        assert alias_contact_email_target[3] == "string"
        assert alias_contact_backups[3] == "array"
        assert alias_contact_backup_primary[3] == "alias"
        assert alias_contact_backup_primary_target[3] == "string"
        assert alias_contact_backup_reviewer[3] == "string"
        assert anchored_template[3] == "object"
        assert anchored_template_email[3] == "string"
        assert anchored_template_flags[3] == "array"
        assert anchored_template_flag_secret[3] == "string"
        assert phone_template[3] == "object"
        assert phone_template_phone[3] == "string"
        assert alias_template[3] == "alias"
        assert alias_template_email[3] == "alias"
        assert alias_template_flags[3] == "alias"
        assert alias_template_flag_secret[3] == "alias"
        assert alias_template_chain[3] == "alias"
        assert alias_template_chain_email[3] == "alias"
        assert alias_template_chain_flags[3] == "alias"
        assert alias_template_chain_flag_secret[3] == "alias"
        assert alias_template_deep[3] == "alias"
        assert alias_template_deep_email[3] == "alias"
        assert alias_template_deep_flags[3] == "alias"
        assert alias_template_deep_flag_secret[3] == "alias"
        assert merged_template[3] == "object"
        assert merged_template_email[3] == "alias"
        assert merged_template_flags[3] == "alias"
        assert merged_template_flag_secret[3] == "alias"
        assert merged_template_team[3] == "string"
        assert merged_template_chain[3] == "object"
        assert merged_template_chain_email[3] == "alias"
        assert merged_template_chain_flags[3] == "alias"
        assert merged_template_chain_flag_secret[3] == "alias"
        assert merged_template_chain_team[3] == "string"
        assert merged_array_template[3] == "object"
        assert merged_array_template_email[3] == "alias"
        assert merged_array_template_flags[3] == "alias"
        assert merged_array_template_phone[3] == "alias"
        assert merged_array_template_team[3] == "string"
        assert scalar_alias_chain[3] == "alias"
        assert scalar_alias_chain_target[3] == "string"
        assert scalar_alias_deep[3] == "alias"
        assert scalar_alias_deep_target[3] == "string"
        assert block_aliases[3] == "array"
        assert block_alias_primary[3] == "alias"
        assert block_alias_primary_target[3] == "string"
        assert block_alias_reviewer[3] == "string"
        assert block_alias_field[3] == "array"
        assert block_alias_field_email[3] == "alias"
        assert block_alias_field_email_target[3] == "string"
        assert block_merge_items[3] == "array"
        assert block_merge_template[3] == block_merge_item[3] == "object"
        assert block_merge_template_email[3] == block_merge_template_label[3] == "string"
        assert block_merge_item_email[3] == "alias"
        assert block_merge_item_label[3] == "string"
        assert content[alias_owner[1] : alias_owner[1] + alias_owner[2]] == b"*primary"
        assert content[
            alias_owner_target[1] : alias_owner_target[1] + alias_owner_target[2]
        ] == b"tagged@example.com"
        assert content[
            alias_contact_email[1] : alias_contact_email[1] + alias_contact_email[2]
        ] == b"*primary"
        assert content[
            alias_contact_email_target[1] : alias_contact_email_target[1] + alias_contact_email_target[2]
        ] == b"tagged@example.com"
        assert content[
            alias_contact_backup_primary[1] : alias_contact_backup_primary[1] + alias_contact_backup_primary[2]
        ] == b"*primary"
        assert content[
            alias_contact_backup_primary_target[1] : alias_contact_backup_primary_target[1] + alias_contact_backup_primary_target[2]
        ] == b"tagged@example.com"
        assert content[
            anchored_template_email[1] : anchored_template_email[1] + anchored_template_email[2]
        ] == b"template@example.com"
        assert content[
            anchored_template_flag_secret[1] : anchored_template_flag_secret[1] + anchored_template_flag_secret[2]
        ] == b"sensitive-template"
        assert content[
            phone_template_phone[1] : phone_template_phone[1] + phone_template_phone[2]
        ] == b"555-0100"
        assert content[
            alias_template[1] : alias_template[1] + alias_template[2]
        ] == b"*contact_template"
        assert (
            alias_template_email[1],
            alias_template_email[2],
            alias_template_flags[1],
            alias_template_flags[2],
            alias_template_flag_secret[1],
            alias_template_flag_secret[2],
        ) == (
            alias_template[1],
            alias_template[2],
            alias_template[1],
            alias_template[2],
            alias_template[1],
            alias_template[2],
        )
        assert content[
            alias_template_chain[1] : alias_template_chain[1] + alias_template_chain[2]
        ] == b"*contact_template"
        assert (
            alias_template_chain_email[1],
            alias_template_chain_email[2],
            alias_template_chain_flags[1],
            alias_template_chain_flags[2],
            alias_template_chain_flag_secret[1],
            alias_template_chain_flag_secret[2],
        ) == (
            alias_template_chain[1],
            alias_template_chain[2],
            alias_template_chain[1],
            alias_template_chain[2],
            alias_template_chain[1],
            alias_template_chain[2],
        )
        assert content[
            alias_template_deep[1] : alias_template_deep[1] + alias_template_deep[2]
        ] == b"*template_alias"
        assert (
            alias_template_deep_email[1],
            alias_template_deep_email[2],
            alias_template_deep_flags[1],
            alias_template_deep_flags[2],
            alias_template_deep_flag_secret[1],
            alias_template_deep_flag_secret[2],
        ) == (
            alias_template_deep[1],
            alias_template_deep[2],
            alias_template_deep[1],
            alias_template_deep[2],
            alias_template_deep[1],
            alias_template_deep[2],
        )
        assert content[
            merged_template[1] : merged_template[1] + merged_template[2]
        ] == b"{<<: *contact_template, team: merged-team}"
        assert content[
            merged_template_email[1] : merged_template_email[1] + merged_template_email[2]
        ] == b"*contact_template"
        assert content[
            merged_template_team[1] : merged_template_team[1] + merged_template_team[2]
        ] == b"merged-team"
        assert (
            merged_template_email[1],
            merged_template_email[2],
            merged_template_flags[1],
            merged_template_flags[2],
            merged_template_flag_secret[1],
            merged_template_flag_secret[2],
        ) == (
            merged_template_email[1],
            merged_template_email[2],
            merged_template_email[1],
            merged_template_email[2],
            merged_template_email[1],
            merged_template_email[2],
        )
        assert content[
            merged_template_chain[1] : merged_template_chain[1]
            + merged_template_chain[2]
        ] == b"{<<: *template_alias, team: chain-team}"
        assert content[
            merged_template_chain_email[1] : merged_template_chain_email[1]
            + merged_template_chain_email[2]
        ] == b"*template_alias"
        assert content[
            merged_template_chain_flags[1] : merged_template_chain_flags[1]
            + merged_template_chain_flags[2]
        ] == b"*template_alias"
        assert content[
            merged_template_chain_flag_secret[1] : merged_template_chain_flag_secret[1]
            + merged_template_chain_flag_secret[2]
        ] == b"*template_alias"
        assert content[
            merged_template_chain_team[1] : merged_template_chain_team[1]
            + merged_template_chain_team[2]
        ] == b"chain-team"
        assert content[
            merged_array_template[1] : merged_array_template[1] + merged_array_template[2]
        ] == b"{<<: [*contact_template, *phone_template], team: array-team}"
        assert content[
            merged_array_template_email[1] : merged_array_template_email[1] + merged_array_template_email[2]
        ] == b"*contact_template"
        assert content[
            merged_array_template_flags[1] : merged_array_template_flags[1] + merged_array_template_flags[2]
        ] == b"*contact_template"
        assert content[
            merged_array_template_phone[1] : merged_array_template_phone[1] + merged_array_template_phone[2]
        ] == b"*phone_template"
        assert content[
            merged_array_template_team[1] : merged_array_template_team[1] + merged_array_template_team[2]
        ] == b"array-team"
        assert content[
            scalar_alias_chain[1] : scalar_alias_chain[1] + scalar_alias_chain[2]
        ] == b"*primary"
        assert content[
            scalar_alias_chain_target[1] : scalar_alias_chain_target[1]
            + scalar_alias_chain_target[2]
        ] == b"tagged@example.com"
        assert content[
            scalar_alias_deep[1] : scalar_alias_deep[1] + scalar_alias_deep[2]
        ] == b"*secondary"
        assert content[
            scalar_alias_deep_target[1] : scalar_alias_deep_target[1]
            + scalar_alias_deep_target[2]
        ] == b"tagged@example.com"
        assert content[
            block_alias_primary[1] : block_alias_primary[1] + block_alias_primary[2]
        ] == b"*primary"
        assert content[
            block_alias_primary_target[1] : block_alias_primary_target[1] + block_alias_primary_target[2]
        ] == b"tagged@example.com"
        assert content[
            block_alias_field_email[1] : block_alias_field_email[1] + block_alias_field_email[2]
        ] == b"*primary"
        assert content[
            block_alias_field_email_target[1] : block_alias_field_email_target[1] + block_alias_field_email_target[2]
        ] == b"tagged@example.com"
        assert content[
            block_merge_template_email[1] : block_merge_template_email[1] + block_merge_template_email[2]
        ] == b"block-template@example.com"
        assert content[
            block_merge_template_label[1] : block_merge_template_label[1] + block_merge_template_label[2]
        ] == b"inherited"
        assert content[
            block_merge_item_email[1] : block_merge_item_email[1] + block_merge_item_email[2]
        ] == b"*block_template"
        assert content[
            block_merge_item_label[1] : block_merge_item_label[1] + block_merge_item_label[2]
        ] == b"explicit"

        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.owner.email",
            "pii",
            "true",
            "policy_agent",
            tool_call_id="tc_markdown_field_label",
        )
        assert fs.fragment_policy_labels(node_id=node_id)[0][0:5] == (
            node_id,
            nested_offset,
            nested_length,
            "pii",
            "true",
        )
        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(node_id, nested_offset, nested_length)
            assert False, "markdown field policy should block the field value range"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:note") == []
        fs.set_policy_rule(
            "confidential",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.summary",
            "confidential",
            "true",
            "policy_agent",
        )
        try:
            fs.read_node_range(node_id, summary_span[1], summary_span[2])
            assert False, "markdown block scalar policy should block the block range"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_policy_rule(
            "tag-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.tags[1]",
            "tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, first_tag[1], first_tag[2])) == b"public"
        try:
            fs.read_node_range(node_id, secret_tag[1], secret_tag[2])
            assert False, "markdown inline sequence item policy should block only the item"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-null-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.missing_values[1]",
            "yaml-null-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, missing_present[1], missing_present[2])
        ) == b"present"
        try:
            fs.read_node_range(node_id, missing_tilde[1], missing_tilde[2])
            assert False, "YAML null item policy should block only that null item"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-core-tag-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.typed_values[5]",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, typed_int[1], typed_int[2])) == b"7"
        try:
            fs.read_node_range(node_id, typed_uri_str[1], typed_uri_str[2])
            assert False, "YAML core tag policy should block only that typed payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.typed_values[6]",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, typed_binary_item[1], typed_binary_item[2])) == (
            b"SGVsbG8="
        )
        try:
            fs.read_node_range(node_id, typed_uri_timestamp[1], typed_uri_timestamp[2])
            assert False, "YAML timestamp tag policy should block only that typed payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.typed_set",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, typed_omap[1], typed_omap[2])) == (
            b"[{first: one}, {second: two}]"
        )
        try:
            fs.read_node_range(node_id, typed_set[1], typed_set[2])
            assert False, "YAML set tag policy should block only that container payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.date_values[0]",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, quoted_date_value[1], quoted_date_value[2])
        ) == b"2026-06-10"
        try:
            fs.read_node_range(node_id, date_value[1], date_value[2])
            assert False, "implicit YAML timestamp policy should block only that payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_policy_rule(
            "contact-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contact.email",
            "contact-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, contact_team[1], contact_team[2])
        ) == b"legal"
        try:
            fs.read_node_range(node_id, contact_email[1], contact_email[2])
            assert False, "markdown inline object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "quoted-contact-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            'frontmatter.quoted_contact["email.addr"]',
            "quoted-contact-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, quoted_contact_team[1], quoted_contact_team[2])
        ) == b"ops"
        try:
            fs.read_node_range(
                node_id,
                quoted_contact_email[1],
                quoted_contact_email[2],
            )
            assert False, "quoted markdown inline object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "inline-sequence-object-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            'frontmatter.contacts[1]["email.addr"]',
            "inline-sequence-object-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, second_contact_team[1], second_contact_team[2])
        ) == b"ops"
        try:
            fs.read_node_range(
                node_id,
                second_contact_email[1],
                second_contact_email[2],
            )
            assert False, "inline sequence object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "nested-inline-object-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contact.manager.email",
            "nested-inline-object-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, contact_manager_team[1], contact_manager_team[2])
        ) == b"privacy"
        try:
            fs.read_node_range(
                node_id,
                contact_manager_email[1],
                contact_manager_email[2],
            )
            assert False, "nested inline object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "nested-sequence-object-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contacts[0].meta.risk",
            "nested-sequence-object-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                first_contact_meta_owner[1],
                first_contact_meta_owner[2],
            )
        ) == b"Ada"
        try:
            fs.read_node_range(
                node_id,
                first_contact_meta_risk[1],
                first_contact_meta_risk[2],
            )
            assert False, "nested inline sequence object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "nested-inline-array-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contacts[0].meta.flags[1]",
            "nested-inline-array-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                first_contact_meta_flag_urgent[1],
                first_contact_meta_flag_urgent[2],
            )
        ) == b"urgent"
        try:
            fs.read_node_range(
                node_id,
                first_contact_meta_flag_restricted[1],
                first_contact_meta_flag_restricted[2],
            )
            assert False, "nested inline array item policy should block only the item"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "decorated-yaml-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.decorated_contact.flags[1]",
            "decorated-yaml-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                decorated_contact_flag_review[1],
                decorated_contact_flag_review[2],
            )
        ) == b"review"
        try:
            fs.read_node_range(
                node_id,
                decorated_contact_flag_secret[1],
                decorated_contact_flag_secret[2],
            )
            assert False, "decorated YAML sequence item policy should block only the payload"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-alias-token-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.alias_contact.backups[0]",
            "yaml-alias-token-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                alias_contact_backup_reviewer[1],
                alias_contact_backup_reviewer[2],
            )
        ) == b"reviewer"
        try:
            fs.read_node_range(
                node_id,
                alias_contact_backup_primary[1],
                alias_contact_backup_primary[2],
            )
            assert False, "YAML alias token policy should block only the alias token"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-scalar-alias-target-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.alias_contact.email.__target",
            "yaml-scalar-alias-target-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, alias_contact_email[1], alias_contact_email[2])
        ) == b"*primary"
        try:
            fs.read_node_range(
                node_id,
                alias_contact_email_target[1],
                alias_contact_email_target[2],
            )
            assert False, "scalar alias target policy should block the anchor payload"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-alias-expanded-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.alias_template.email",
            "yaml-alias-expanded-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                anchored_template_email[1],
                anchored_template_email[2],
            )
        ) == b"template@example.com"
        try:
            fs.read_node_range(node_id, alias_template[1], alias_template[2])
            assert False, "expanded YAML alias field policy should block the alias token"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-merge-key-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.merged_template.email",
            "yaml-merge-key-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                anchored_template_email[1],
                anchored_template_email[2],
            )
        ) == b"template@example.com"
        assert bytes(
            fs.read_node_range(
                node_id,
                merged_template_team[1],
                merged_template_team[2],
            )
        ) == b"merged-team"
        try:
            fs.read_node_range(
                node_id,
                merged_template_email[1],
                merged_template_email[2],
            )
            assert False, "YAML merge key policy should block only the merge alias token"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-merge-array-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.merged_array_template.phone",
            "yaml-merge-array-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                merged_array_template_email[1],
                merged_array_template_email[2],
            )
        ) == b"*contact_template"
        assert bytes(
            fs.read_node_range(
                node_id,
                phone_template_phone[1],
                phone_template_phone[2],
            )
        ) == b"555-0100"
        assert bytes(
            fs.read_node_range(
                node_id,
                merged_array_template_team[1],
                merged_array_template_team[2],
            )
        ) == b"array-team"
        try:
            fs.read_node_range(
                node_id,
                merged_array_template_phone[1],
                merged_array_template_phone[2],
            )
            assert False, "YAML merge array policy should block only that merge alias token"
        except anfs_core.PolicyDeniedError:
            pass

        try:
            fs.set_markdown_field_policy_label(
                node_id,
                "frontmatter.missing",
                "pii",
                "true",
                "policy_agent",
            )
            assert False, "missing Markdown field path should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        plain_node = ws.write("plain.md", b"# No frontmatter\n", [])
        try:
            fs.markdown_field_spans(plain_node)
            assert False, "Markdown without frontmatter should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_markdown_body_section_policy_labels_map_headings_to_fragments():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = (
            b"---\n"
            b"title: Contract note\n"
            b"---\n"
            b"# Overview\n"
            b"Public summary.\n"
            b"## Private Notes\n"
            b"Sensitive renewal strategy.\n"
            b"Internal paragraph line two.\n"
            b"\n"
            b"- Escalate internally\n"
            b"- Review privilege\n"
            b"\n"
            b"| Key | Value |\n"
            b"| --- | --- |\n"
            b"| Tier | Secret |\n"
            b"<div class=\"secret\">\n"
            b"Secret HTML note\n"
            b"</div>\n"
            b"\n"
            b"```text\n"
            b"# Not A Heading\n"
            b"```\n"
            b"---\n"
            b"    # Indented Not A Heading\n"
            b"    secret_indented()\n"
            b"### Nested Detail\n"
            b"Still private.\n"
            b"## Appendix\n"
            b"Public appendix.\n"
        )
        node_id = ws.write("sections.md", content, [])
        ws.publish("sections.md", "artifact:sections@v1")

        spans = fs.markdown_section_spans(node_id)
        span_by_path = {row[0]: row for row in spans}
        assert "body.not-a-heading" not in span_by_path
        assert span_by_path["body.overview"][3] == "h1"
        assert span_by_path["body.overview.paragraph.1"][3] == "paragraph"
        private_path, private_offset, private_length, private_kind = span_by_path[
            "body.private-notes"
        ]
        assert (private_path, private_kind) == ("body.private-notes", "h2")
        assert content[private_offset : private_offset + private_length].startswith(
            b"## Private Notes\n"
        )
        assert b"### Nested Detail\nStill private." in content[
            private_offset : private_offset + private_length
        ]
        assert b"## Appendix" not in content[private_offset : private_offset + private_length]
        paragraph_span = span_by_path["body.private-notes.paragraph.1"]
        assert paragraph_span[3] == "paragraph"
        assert b"Sensitive renewal strategy." in content[
            paragraph_span[1] : paragraph_span[1] + paragraph_span[2]
        ]
        first_paragraph_line = span_by_path["body.private-notes.paragraph.1.line.1"]
        second_paragraph_line = span_by_path["body.private-notes.paragraph.1.line.2"]
        assert first_paragraph_line[3] == second_paragraph_line[3] == "paragraph-line"
        assert content[
            first_paragraph_line[1] : first_paragraph_line[1] + first_paragraph_line[2]
        ] == b"Sensitive renewal strategy.\n"
        assert content[
            second_paragraph_line[1] : second_paragraph_line[1] + second_paragraph_line[2]
        ] == b"Internal paragraph line two.\n"
        list_span = span_by_path["body.private-notes.list.1"]
        assert list_span[3] == "list"
        assert b"Review privilege" in content[list_span[1] : list_span[1] + list_span[2]]
        list_item_span = span_by_path["body.private-notes.list.1.item.2"]
        assert list_item_span[3] == "list-item"
        assert b"Review privilege" in content[
            list_item_span[1] : list_item_span[1] + list_item_span[2]
        ]
        table_span = span_by_path["body.private-notes.table.1"]
        assert table_span[3] == "table"
        assert b"Tier | Secret" in content[table_span[1] : table_span[1] + table_span[2]]
        table_header_row = span_by_path["body.private-notes.table.1.row.1"]
        table_secret_row = span_by_path["body.private-notes.table.1.row.2"]
        assert table_header_row[3] == table_secret_row[3] == "table-row"
        assert b"Key | Value" in content[
            table_header_row[1] : table_header_row[1] + table_header_row[2]
        ]
        assert b"Tier | Secret" in content[
            table_secret_row[1] : table_secret_row[1] + table_secret_row[2]
        ]
        assert "body.private-notes.table.1.row.3" not in span_by_path
        html_span = span_by_path["body.private-notes.html.1"]
        assert html_span[3] == "html"
        assert b"Secret HTML note" in content[html_span[1] : html_span[1] + html_span[2]]
        code_span = span_by_path["body.private-notes.code.1"]
        assert code_span[3] == "code"
        assert b"# Not A Heading" in content[code_span[1] : code_span[1] + code_span[2]]
        thematic_span = span_by_path["body.private-notes.thematic-break.1"]
        assert thematic_span[3] == "thematic-break"
        assert content[thematic_span[1] : thematic_span[1] + thematic_span[2]] == b"---\n"
        indented_code_span = span_by_path["body.private-notes.code.2"]
        assert indented_code_span[3] == "code"
        assert b"# Indented Not A Heading" in content[
            indented_code_span[1] : indented_code_span[1] + indented_code_span[2]
        ]
        assert "body.indented-not-a-heading" not in span_by_path
        nested_paragraph = span_by_path["body.nested-detail.paragraph.1"]
        assert nested_paragraph[3] == "paragraph"

        fs.set_markdown_section_policy_label(
            node_id,
            "body.private-notes",
            "sensitivity",
            "restricted",
            "policy_agent",
            tool_call_id="tc_markdown_section_label",
        )
        assert fs.fragment_policy_labels(node_id=node_id)[0][0:5] == (
            node_id,
            private_offset,
            private_length,
            "sensitivity",
            "restricted",
        )

        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        overview_offset = content.index(b"# Overview")
        assert bytes(fs.read_node_range(node_id, overview_offset, len(b"# Overview"))) == b"# Overview"
        try:
            fs.read_node_range(node_id, private_offset, len(b"## Private Notes"))
            assert False, "markdown section policy should block the section range"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:sections") == []

        paragraph_line_content = (
            b"# Paragraph Lines\n"
            b"Public paragraph line one.\n"
            b"Secret paragraph line two.\n"
            b"\n"
            b"- Public list after paragraph\n"
        )
        paragraph_line_node = ws.write("paragraph-line.md", paragraph_line_content, [])
        paragraph_line_spans = {
            row[0]: row for row in fs.markdown_section_spans(paragraph_line_node)
        }
        paragraph_line_one = paragraph_line_spans[
            "body.paragraph-lines.paragraph.1.line.1"
        ]
        paragraph_line_two = paragraph_line_spans[
            "body.paragraph-lines.paragraph.1.line.2"
        ]
        fs.set_markdown_section_policy_label(
            paragraph_line_node,
            "body.paragraph-lines.paragraph.1.line.2",
            "paragraph-line-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "paragraph-line-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                paragraph_line_node,
                paragraph_line_one[1],
                len(b"Public paragraph line one."),
            )
        ) == b"Public paragraph line one."
        try:
            fs.read_node_range(
                paragraph_line_node,
                paragraph_line_two[1],
                len(b"Secret paragraph line two."),
            )
            assert False, "markdown paragraph line policy should block only the line"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                paragraph_line_node,
                paragraph_line_content.index(b"- Public list after paragraph"),
                len(b"- Public list after paragraph"),
            )
        ) == b"- Public list after paragraph"

        inline_emphasis_content = (
            b"# Inline Emphasis\n"
            b"Public *safe emphasis* and **secret strong** remain.\n"
            b"Private _secret emphasis_ and __public strong__ remain.\n"
            b"Code `*hidden emphasis*` then *outside emphasis*.\n"
        )
        inline_emphasis_node = ws.write(
            "inline-emphasis.md", inline_emphasis_content, []
        )
        inline_emphasis_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_emphasis_node)
        }
        safe_emphasis = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.emphasis.1"
        ]
        safe_emphasis_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.emphasis.1.text"
        ]
        secret_strong = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.strong.1"
        ]
        secret_strong_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.strong.1.text"
        ]
        secret_emphasis_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.2.emphasis.1.text"
        ]
        public_strong_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.2.strong.1.text"
        ]
        outside_emphasis = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.3.emphasis.1"
        ]
        assert safe_emphasis[3] == "inline-emphasis"
        assert safe_emphasis_text[3] == secret_emphasis_text[3] == "inline-emphasis-text"
        assert secret_strong[3] == "inline-strong"
        assert secret_strong_text[3] == public_strong_text[3] == "inline-strong-text"
        assert inline_emphasis_content[
            safe_emphasis[1] : safe_emphasis[1] + safe_emphasis[2]
        ] == b"*safe emphasis*"
        assert inline_emphasis_content[
            safe_emphasis_text[1] : safe_emphasis_text[1] + safe_emphasis_text[2]
        ] == b"safe emphasis"
        assert inline_emphasis_content[
            secret_strong[1] : secret_strong[1] + secret_strong[2]
        ] == b"**secret strong**"
        assert inline_emphasis_content[
            secret_strong_text[1] : secret_strong_text[1] + secret_strong_text[2]
        ] == b"secret strong"
        assert inline_emphasis_content[
            secret_emphasis_text[1] : secret_emphasis_text[1] + secret_emphasis_text[2]
        ] == b"secret emphasis"
        assert inline_emphasis_content[
            public_strong_text[1] : public_strong_text[1] + public_strong_text[2]
        ] == b"public strong"
        assert inline_emphasis_content[
            outside_emphasis[1] : outside_emphasis[1] + outside_emphasis[2]
        ] == b"*outside emphasis*"
        assert "body.inline-emphasis.paragraph.1.line.3.emphasis.2" not in inline_emphasis_spans
        fs.set_markdown_section_policy_label(
            inline_emphasis_node,
            "body.inline-emphasis.paragraph.1.line.1.strong.1.text",
            "inline-strong-text-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-strong-text-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_emphasis_node,
                safe_emphasis_text[1],
                len(b"safe emphasis"),
            )
        ) == b"safe emphasis"
        assert bytes(
            fs.read_node_range(inline_emphasis_node, secret_strong[1], len(b"**"))
        ) == b"**"
        try:
            fs.read_node_range(
                inline_emphasis_node,
                secret_strong_text[1],
                len(b"secret strong"),
            )
            assert False, "markdown inline strong text policy should block only the text"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_emphasis_node,
                inline_emphasis_content.index(b" remain."),
                len(b" remain."),
            )
        ) == b" remain."

        escaped_inline_content = (
            b"# Escaped Inline\n"
            b"Literal \\*not emphasis\\* then *real emphasis*.\n"
            b"Literal \\**not strong\\** then **real strong**.\n"
            b"Literal \\[not link](https://example.test/no) then [real link](https://example.test/yes).\n"
            b"Label [literal \\] bracket](https://example.test/label) remains.\n"
            b"Image literal \\![not image] then ![real image](https://example.test/yes.png).\n"
            b"Code escaped \\`not code\\` then `real code`.\n"
            b"Literal \\<https://example.test/no> then <https://example.test/yes>.\n"
            b"Escaped reference \\[not ref][doc] then [real ref][doc].\n"
            b"[doc]: https://example.test/doc\n"
        )
        escaped_inline_node = ws.write("escaped-inline.md", escaped_inline_content, [])
        escaped_inline_spans = {
            row[0]: row for row in fs.markdown_section_spans(escaped_inline_node)
        }
        real_emphasis = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.1.emphasis.1"
        ]
        real_strong = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.2.strong.1"
        ]
        real_link = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.3.link.1"
        ]
        escaped_bracket_label = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.4.link.1.label"
        ]
        real_image = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.5.image.1"
        ]
        real_code = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.6.code.1"
        ]
        real_autolink = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.7.autolink.1"
        ]
        real_reference = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.8.reference-link.1"
        ]
        assert escaped_inline_content[
            real_emphasis[1] : real_emphasis[1] + real_emphasis[2]
        ] == b"*real emphasis*"
        assert escaped_inline_content[
            real_strong[1] : real_strong[1] + real_strong[2]
        ] == b"**real strong**"
        assert escaped_inline_content[
            real_link[1] : real_link[1] + real_link[2]
        ] == b"[real link](https://example.test/yes)"
        assert escaped_inline_content[
            escaped_bracket_label[1] : escaped_bracket_label[1] + escaped_bracket_label[2]
        ] == b"literal \\] bracket"
        assert escaped_inline_content[
            real_image[1] : real_image[1] + real_image[2]
        ] == b"![real image](https://example.test/yes.png)"
        assert escaped_inline_content[
            real_code[1] : real_code[1] + real_code[2]
        ] == b"`real code`"
        assert escaped_inline_content[
            real_autolink[1] : real_autolink[1] + real_autolink[2]
        ] == b"<https://example.test/yes>"
        assert escaped_inline_content[
            real_reference[1] : real_reference[1] + real_reference[2]
        ] == b"[real ref][doc]"
        assert "body.escaped-inline.paragraph.1.line.1.emphasis.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.2.strong.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.3.link.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.5.image.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.6.code.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.7.autolink.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.8.reference-link.2" not in escaped_inline_spans

        inline_link_content = (
            b"# Inline Links\n"
            b"Public context [safe link](https://example.test/public).\n"
            b"Secret pointer [private link](https://example.test/private) remains.\n"
            b"Image marker ![not a link](https://example.test/image.png) stays plain.\n"
        )
        inline_link_node = ws.write("inline-link.md", inline_link_content, [])
        inline_link_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_link_node)
        }
        safe_link = inline_link_spans["body.inline-links.paragraph.1.line.1.link.1"]
        private_link = inline_link_spans[
            "body.inline-links.paragraph.1.line.2.link.1"
        ]
        safe_link_label = inline_link_spans[
            "body.inline-links.paragraph.1.line.1.link.1.label"
        ]
        private_link_label = inline_link_spans[
            "body.inline-links.paragraph.1.line.2.link.1.label"
        ]
        safe_link_destination = inline_link_spans[
            "body.inline-links.paragraph.1.line.1.link.1.destination"
        ]
        private_link_destination = inline_link_spans[
            "body.inline-links.paragraph.1.line.2.link.1.destination"
        ]
        assert safe_link[3] == private_link[3] == "inline-link"
        assert safe_link_label[3] == private_link_label[3] == "inline-link-label"
        assert (
            safe_link_destination[3]
            == private_link_destination[3]
            == "inline-link-destination"
        )
        assert "body.inline-links.paragraph.1.line.3.link.1" not in inline_link_spans
        assert inline_link_content[safe_link[1] : safe_link[1] + safe_link[2]] == (
            b"[safe link](https://example.test/public)"
        )
        assert inline_link_content[
            private_link[1] : private_link[1] + private_link[2]
        ] == b"[private link](https://example.test/private)"
        assert inline_link_content[
            safe_link_label[1] : safe_link_label[1] + safe_link_label[2]
        ] == b"safe link"
        assert inline_link_content[
            private_link_label[1] : private_link_label[1] + private_link_label[2]
        ] == b"private link"
        assert inline_link_content[
            safe_link_destination[1] : safe_link_destination[1] + safe_link_destination[2]
        ] == b"https://example.test/public"
        assert inline_link_content[
            private_link_destination[1] : private_link_destination[1] + private_link_destination[2]
        ] == b"https://example.test/private"
        fs.set_markdown_section_policy_label(
            inline_link_node,
            "body.inline-links.paragraph.1.line.2.link.1",
            "inline-link-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(inline_link_node, safe_link[1], len(b"[safe link]"))
        ) == b"[safe link]"
        assert bytes(
            fs.read_node_range(
                inline_link_node,
                inline_link_content.index(b"Secret pointer"),
                len(b"Secret pointer"),
            )
        ) == b"Secret pointer"
        try:
            fs.read_node_range(inline_link_node, private_link[1], len(b"[private link]"))
            assert False, "markdown inline link policy should block only the link"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_node,
                inline_link_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_link_label_content = (
            b"# Inline Link Labels\n"
            b"Public wrapper [secret label](https://example.test/public-target) remains.\n"
        )
        inline_link_label_node = ws.write(
            "inline-link-label.md", inline_link_label_content, []
        )
        inline_link_label_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_link_label_node)
        }
        secret_label = inline_link_label_spans[
            "body.inline-link-labels.paragraph.1.line.1.link.1.label"
        ]
        public_destination = inline_link_label_spans[
            "body.inline-link-labels.paragraph.1.line.1.link.1.destination"
        ]
        assert secret_label[3] == "inline-link-label"
        assert public_destination[3] == "inline-link-destination"
        assert inline_link_label_content[
            secret_label[1] : secret_label[1] + secret_label[2]
        ] == b"secret label"
        fs.set_markdown_section_policy_label(
            inline_link_label_node,
            "body.inline-link-labels.paragraph.1.line.1.link.1.label",
            "inline-link-label-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-label-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_link_label_node,
                public_destination[1],
                len(b"https://example.test/public-target"),
            )
        ) == b"https://example.test/public-target"
        try:
            fs.read_node_range(
                inline_link_label_node,
                secret_label[1],
                len(b"secret label"),
            )
            assert False, "markdown inline link label policy should block only the label"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_label_node,
                inline_link_label_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_link_destination_content = (
            b"# Inline Link Destinations\n"
            b"Public label [secret destination](https://example.test/secret-destination) remains.\n"
        )
        inline_link_destination_node = ws.write(
            "inline-link-destination.md", inline_link_destination_content, []
        )
        inline_link_destination_spans = {
            row[0]: row
            for row in fs.markdown_section_spans(inline_link_destination_node)
        }
        secret_destination = inline_link_destination_spans[
            "body.inline-link-destinations.paragraph.1.line.1.link.1.destination"
        ]
        assert secret_destination[3] == "inline-link-destination"
        assert inline_link_destination_content[
            secret_destination[1] : secret_destination[1] + secret_destination[2]
        ] == b"https://example.test/secret-destination"
        fs.set_markdown_section_policy_label(
            inline_link_destination_node,
            "body.inline-link-destinations.paragraph.1.line.1.link.1.destination",
            "inline-link-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_link_destination_node,
                inline_link_destination_content.index(b"[secret destination]"),
                len(b"[secret destination]"),
            )
        ) == b"[secret destination]"
        try:
            fs.read_node_range(
                inline_link_destination_node,
                secret_destination[1],
                len(b"https://example.test/secret-destination"),
            )
            assert False, "markdown inline link destination policy should block only the destination"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_destination_node,
                inline_link_destination_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_link_title_content = (
            b"# Inline Link Titles\n"
            b"Public wrapper [secret title](https://example.test/title-target \"Secret Title\") remains.\n"
            b"Paren wrapper [paren title](https://example.test/paren-target (Paren Link Title)) remains.\n"
        )
        inline_link_title_node = ws.write(
            "inline-link-title.md", inline_link_title_content, []
        )
        inline_link_title_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_link_title_node)
        }
        title_destination = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.1.link.1.destination"
        ]
        secret_title = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.1.link.1.title"
        ]
        paren_title_destination = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.2.link.1.destination"
        ]
        paren_title = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.2.link.1.title"
        ]
        assert title_destination[3] == "inline-link-destination"
        assert paren_title_destination[3] == "inline-link-destination"
        assert secret_title[3] == paren_title[3] == "inline-link-title"
        assert inline_link_title_content[
            title_destination[1] : title_destination[1] + title_destination[2]
        ] == b"https://example.test/title-target"
        assert inline_link_title_content[
            secret_title[1] : secret_title[1] + secret_title[2]
        ] == b"Secret Title"
        assert inline_link_title_content[
            paren_title_destination[1] : paren_title_destination[1]
            + paren_title_destination[2]
        ] == b"https://example.test/paren-target"
        assert inline_link_title_content[
            paren_title[1] : paren_title[1] + paren_title[2]
        ] == b"Paren Link Title"
        fs.set_markdown_section_policy_label(
            inline_link_title_node,
            "body.inline-link-titles.paragraph.1.line.1.link.1.title",
            "inline-link-title-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-title-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_link_title_node,
                title_destination[1],
                len(b"https://example.test/title-target"),
            )
        ) == b"https://example.test/title-target"
        try:
            fs.read_node_range(
                inline_link_title_node,
                secret_title[1],
                len(b"Secret Title"),
            )
            assert False, "markdown inline link title policy should block only the title"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_title_node,
                inline_link_title_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        nested_destination_content = (
            b"# Nested Destinations\n"
            b"Link [nested](https://example.test/report(v2)/final) remains.\n"
            b"Titled [nested title](https://example.test/report(v2) \"Nested Title\") remains.\n"
            b"Image ![nested image](https://example.test/chart(v2).png) remains.\n"
            b"Escaped [literal parens](https://example.test/a\\(literal\\)) remains.\n"
            b"Angle [angle destination](<https://example.test/angle path>) remains.\n"
            b"Angle title [angle title](<https://example.test/angle-title> \"Angle Title\") remains.\n"
            b"Angle image ![angle image](<https://example.test/angle image.png>) remains.\n"
        )
        nested_destination_node = ws.write(
            "nested-destination.md", nested_destination_content, []
        )
        nested_destination_spans = {
            row[0]: row for row in fs.markdown_section_spans(nested_destination_node)
        }
        nested_link = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.1.link.1"
        ]
        nested_link_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.1.link.1.destination"
        ]
        nested_titled_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.2.link.1.destination"
        ]
        nested_title = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.2.link.1.title"
        ]
        nested_image_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.3.image.1.destination"
        ]
        escaped_paren_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.4.link.1.destination"
        ]
        angle_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.5.link.1.destination"
        ]
        angle_title_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.6.link.1.destination"
        ]
        angle_title = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.6.link.1.title"
        ]
        angle_image_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.7.image.1.destination"
        ]
        assert nested_link[3] == "inline-link"
        assert (
            nested_link_destination[3]
            == nested_titled_destination[3]
            == angle_destination[3]
            == angle_title_destination[3]
            == "inline-link-destination"
        )
        assert nested_title[3] == "inline-link-title"
        assert angle_title[3] == "inline-link-title"
        assert nested_image_destination[3] == angle_image_destination[3] == "inline-image-destination"
        assert escaped_paren_destination[3] == "inline-link-destination"
        assert nested_destination_content[
            nested_link[1] : nested_link[1] + nested_link[2]
        ] == b"[nested](https://example.test/report(v2)/final)"
        assert nested_destination_content[
            nested_link_destination[1] : nested_link_destination[1] + nested_link_destination[2]
        ] == b"https://example.test/report(v2)/final"
        assert nested_destination_content[
            nested_titled_destination[1] : nested_titled_destination[1]
            + nested_titled_destination[2]
        ] == b"https://example.test/report(v2)"
        assert nested_destination_content[
            nested_title[1] : nested_title[1] + nested_title[2]
        ] == b"Nested Title"
        assert nested_destination_content[
            nested_image_destination[1] : nested_image_destination[1]
            + nested_image_destination[2]
        ] == b"https://example.test/chart(v2).png"
        assert nested_destination_content[
            escaped_paren_destination[1] : escaped_paren_destination[1]
            + escaped_paren_destination[2]
        ] == b"https://example.test/a\\(literal\\)"
        assert nested_destination_content[
            angle_destination[1] : angle_destination[1] + angle_destination[2]
        ] == b"https://example.test/angle path"
        assert nested_destination_content[
            angle_title_destination[1] : angle_title_destination[1]
            + angle_title_destination[2]
        ] == b"https://example.test/angle-title"
        assert nested_destination_content[
            angle_title[1] : angle_title[1] + angle_title[2]
        ] == b"Angle Title"
        assert nested_destination_content[
            angle_image_destination[1] : angle_image_destination[1]
            + angle_image_destination[2]
        ] == b"https://example.test/angle image.png"

        reference_link_content = (
            b"# Reference Links\n"
            b"Public pointer [public label][Public Ref] remains.\n"
            b"Secret pointer [safe label][private ref] remains.\n"
            b"Code `[hidden][private ref]` then [outside][public ref].\n"
            b"Collapsed pointer [collapsed ref][] remains.\n"
            b"Shortcut pointer [shortcut ref] remains.\n"
            b"Parenthesized pointer [paren label][paren ref] remains.\n"
            b"Angle pointer [angle label][angle ref] remains.\n"
            b"Escaped pointer [escaped label][escaped\\] ref] remains.\n"
            b"\n"
            b"[public ref]: https://example.test/public-reference\n"
            b"[private ref]: https://example.test/private-reference \"Secret Reference Title\"\n"
            b"[collapsed ref]: https://example.test/collapsed-reference\n"
            b"[shortcut ref]: https://example.test/shortcut-reference\n"
            b"[paren ref]: https://example.test/paren-reference (Paren Reference Title)\n"
            b"[angle ref]: <https://example.test/angle reference> \"Angle Reference Title\"\n"
            b"[escaped\\] ref]: https://example.test/escaped-reference\n"
        )
        reference_link_node = ws.write("reference-link.md", reference_link_content, [])
        reference_link_spans = {
            row[0]: row for row in fs.markdown_section_spans(reference_link_node)
        }
        public_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.1.reference-link.1"
        ]
        private_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1"
        ]
        private_reference_label = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.label"
        ]
        private_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.reference"
        ]
        private_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.resolved-destination"
        ]
        private_resolved_title = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.resolved-title"
        ]
        private_reference_definition_label = reference_link_spans[
            "body.reference-links.link-reference.2.label"
        ]
        private_reference_definition_destination = reference_link_spans[
            "body.reference-links.link-reference.2.destination"
        ]
        private_reference_definition_title = reference_link_spans[
            "body.reference-links.link-reference.2.title"
        ]
        hidden_reference_code = reference_link_spans[
            "body.reference-links.paragraph.1.line.3.code.1"
        ]
        outside_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.3.reference-link.1"
        ]
        collapsed_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.4.reference-link.1"
        ]
        collapsed_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.4.reference-link.1.reference"
        ]
        collapsed_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.4.reference-link.1.resolved-destination"
        ]
        shortcut_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.5.reference-link.1"
        ]
        shortcut_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.5.reference-link.1.reference"
        ]
        shortcut_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.5.reference-link.1.resolved-destination"
        ]
        paren_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.6.reference-link.1"
        ]
        paren_resolved_title = reference_link_spans[
            "body.reference-links.paragraph.1.line.6.reference-link.1.resolved-title"
        ]
        paren_reference_definition_title = reference_link_spans[
            "body.reference-links.link-reference.5.title"
        ]
        angle_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.7.reference-link.1"
        ]
        angle_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.7.reference-link.1.resolved-destination"
        ]
        angle_resolved_title = reference_link_spans[
            "body.reference-links.paragraph.1.line.7.reference-link.1.resolved-title"
        ]
        angle_reference_definition_destination = reference_link_spans[
            "body.reference-links.link-reference.6.destination"
        ]
        angle_reference_definition_title = reference_link_spans[
            "body.reference-links.link-reference.6.title"
        ]
        escaped_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.8.reference-link.1"
        ]
        escaped_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.8.reference-link.1.reference"
        ]
        escaped_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.8.reference-link.1.resolved-destination"
        ]
        escaped_reference_definition_label = reference_link_spans[
            "body.reference-links.link-reference.7.label"
        ]
        escaped_reference_definition_destination = reference_link_spans[
            "body.reference-links.link-reference.7.destination"
        ]
        assert (
            public_reference_link[3]
            == private_reference_link[3]
            == outside_reference_link[3]
            == collapsed_reference_link[3]
            == shortcut_reference_link[3]
            == paren_reference_link[3]
            == angle_reference_link[3]
            == escaped_reference_link[3]
            == "reference-link"
        )
        assert private_reference_label[3] == "reference-link-label"
        assert private_reference_marker[3] == "reference-link-reference"
        assert (
            private_resolved_destination[3]
            == "reference-link-resolved-destination"
        )
        assert private_resolved_title[3] == "reference-link-resolved-title"
        assert private_reference_definition_label[3] == "link-reference-label"
        assert (
            private_reference_definition_destination[3]
            == "link-reference-destination"
        )
        assert private_reference_definition_title[3] == "link-reference-title"
        assert hidden_reference_code[3] == "inline-code"
        assert (
            "body.reference-links.paragraph.1.line.3.reference-link.2"
            not in reference_link_spans
        )
        assert collapsed_reference_marker[3] == "reference-link-reference"
        assert collapsed_reference_marker[2] == 0
        assert collapsed_resolved_destination[3] == "reference-link-resolved-destination"
        assert shortcut_reference_marker[3] == "reference-link-reference"
        assert shortcut_reference_marker[2] == 0
        assert shortcut_resolved_destination[3] == "reference-link-resolved-destination"
        assert paren_resolved_title[3] == "reference-link-resolved-title"
        assert angle_resolved_destination[3] == "reference-link-resolved-destination"
        assert angle_resolved_title[3] == "reference-link-resolved-title"
        assert paren_reference_definition_title[3] == "link-reference-title"
        assert angle_reference_definition_destination[3] == "link-reference-destination"
        assert angle_reference_definition_title[3] == "link-reference-title"
        assert escaped_reference_marker[3] == "reference-link-reference"
        assert escaped_resolved_destination[3] == "reference-link-resolved-destination"
        assert escaped_reference_definition_label[3] == "link-reference-label"
        assert escaped_reference_definition_destination[3] == "link-reference-destination"
        assert reference_link_content[
            public_reference_link[1] : public_reference_link[1]
            + public_reference_link[2]
        ] == b"[public label][Public Ref]"
        assert reference_link_content[
            private_reference_label[1] : private_reference_label[1]
            + private_reference_label[2]
        ] == b"safe label"
        assert reference_link_content[
            private_reference_marker[1] : private_reference_marker[1]
            + private_reference_marker[2]
        ] == b"private ref"
        assert reference_link_content[
            private_resolved_destination[1] : private_resolved_destination[1]
            + private_resolved_destination[2]
        ] == b"https://example.test/private-reference"
        assert reference_link_content[
            private_resolved_title[1] : private_resolved_title[1]
            + private_resolved_title[2]
        ] == b"Secret Reference Title"
        assert reference_link_content[
            private_reference_definition_label[1] : private_reference_definition_label[1]
            + private_reference_definition_label[2]
        ] == b"private ref"
        assert reference_link_content[
            private_reference_definition_destination[1] : private_reference_definition_destination[1]
            + private_reference_definition_destination[2]
        ] == b"https://example.test/private-reference"
        assert reference_link_content[
            private_reference_definition_title[1] : private_reference_definition_title[1]
            + private_reference_definition_title[2]
        ] == b"Secret Reference Title"
        assert reference_link_content[
            paren_resolved_title[1] : paren_resolved_title[1] + paren_resolved_title[2]
        ] == b"Paren Reference Title"
        assert reference_link_content[
            paren_reference_definition_title[1] : paren_reference_definition_title[1]
            + paren_reference_definition_title[2]
        ] == b"Paren Reference Title"
        assert reference_link_content[
            angle_resolved_destination[1] : angle_resolved_destination[1]
            + angle_resolved_destination[2]
        ] == b"https://example.test/angle reference"
        assert reference_link_content[
            angle_reference_definition_destination[1] : angle_reference_definition_destination[1]
            + angle_reference_definition_destination[2]
        ] == b"https://example.test/angle reference"
        assert reference_link_content[
            angle_resolved_title[1] : angle_resolved_title[1] + angle_resolved_title[2]
        ] == b"Angle Reference Title"
        assert reference_link_content[
            angle_reference_definition_title[1] : angle_reference_definition_title[1]
            + angle_reference_definition_title[2]
        ] == b"Angle Reference Title"
        assert reference_link_content[
            escaped_reference_marker[1] : escaped_reference_marker[1]
            + escaped_reference_marker[2]
        ] == b"escaped\\] ref"
        assert reference_link_content[
            escaped_resolved_destination[1] : escaped_resolved_destination[1]
            + escaped_resolved_destination[2]
        ] == b"https://example.test/escaped-reference"
        assert reference_link_content[
            escaped_reference_definition_label[1] : escaped_reference_definition_label[1]
            + escaped_reference_definition_label[2]
        ] == b"escaped\\] ref"
        assert reference_link_content[
            escaped_reference_definition_destination[1] : escaped_reference_definition_destination[1]
            + escaped_reference_definition_destination[2]
        ] == b"https://example.test/escaped-reference"
        assert reference_link_content[
            hidden_reference_code[1] : hidden_reference_code[1]
            + hidden_reference_code[2]
        ] == b"`[hidden][private ref]`"
        assert reference_link_content[
            collapsed_reference_link[1] : collapsed_reference_link[1]
            + collapsed_reference_link[2]
        ] == b"[collapsed ref][]"
        assert reference_link_content[
            collapsed_resolved_destination[1] : collapsed_resolved_destination[1]
            + collapsed_resolved_destination[2]
        ] == b"https://example.test/collapsed-reference"
        assert reference_link_content[
            shortcut_reference_link[1] : shortcut_reference_link[1]
            + shortcut_reference_link[2]
        ] == b"[shortcut ref]"
        assert reference_link_content[
            shortcut_resolved_destination[1] : shortcut_resolved_destination[1]
            + shortcut_resolved_destination[2]
        ] == b"https://example.test/shortcut-reference"
        fs.set_markdown_section_policy_label(
            reference_link_node,
            "body.reference-links.paragraph.1.line.2.reference-link.1.resolved-destination",
            "reference-link-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "reference-link-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                private_reference_link[1],
                len(b"[safe label][private ref]"),
            )
        ) == b"[safe label][private ref]"
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                private_resolved_title[1],
                len(b"Secret Reference Title"),
            )
        ) == b"Secret Reference Title"
        try:
            fs.read_node_range(
                reference_link_node,
                private_resolved_destination[1],
                len(b"https://example.test/private-reference"),
            )
            assert False, "reference-link resolved destination policy should block only the definition target"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                outside_reference_link[1],
                len(b"[outside][public ref]"),
            )
        ) == b"[outside][public ref]"
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                collapsed_reference_link[1],
                len(b"[collapsed ref][]"),
            )
        ) == b"[collapsed ref][]"
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                shortcut_reference_link[1],
                len(b"[shortcut ref]"),
            )
        ) == b"[shortcut ref]"

        reference_image_content = (
            b"# Reference Images\n"
            b"Public figure ![public chart][Public Image] remains.\n"
            b"Secret figure ![safe alt][private image] remains.\n"
            b"Code `![hidden][private image]` then ![outside][public image].\n"
            b"Collapsed figure ![collapsed image][] remains.\n"
            b"Shortcut figure ![shortcut image] remains.\n"
            b"Angle figure ![angle alt][angle image] remains.\n"
            b"Escaped figure ![escaped alt][escaped\\] image] remains.\n"
            b"\n"
            b"[public image]: https://example.test/public-image.png\n"
            b"[private image]: https://example.test/private-image.png 'Secret Image Reference Title'\n"
            b"[collapsed image]: https://example.test/collapsed-image.png\n"
            b"[shortcut image]: https://example.test/shortcut-image.png\n"
            b"[angle image]: <https://example.test/angle image.png> 'Angle Image Reference Title'\n"
            b"[escaped\\] image]: https://example.test/escaped-image.png\n"
        )
        reference_image_node = ws.write("reference-image.md", reference_image_content, [])
        reference_image_spans = {
            row[0]: row for row in fs.markdown_section_spans(reference_image_node)
        }
        public_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.1.reference-image.1"
        ]
        private_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1"
        ]
        private_reference_alt = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.alt"
        ]
        private_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.reference"
        ]
        private_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.resolved-destination"
        ]
        private_image_resolved_title = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.resolved-title"
        ]
        private_image_definition_destination = reference_image_spans[
            "body.reference-images.link-reference.2.destination"
        ]
        private_image_definition_title = reference_image_spans[
            "body.reference-images.link-reference.2.title"
        ]
        hidden_reference_image_code = reference_image_spans[
            "body.reference-images.paragraph.1.line.3.code.1"
        ]
        outside_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.3.reference-image.1"
        ]
        collapsed_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.4.reference-image.1"
        ]
        collapsed_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.4.reference-image.1.reference"
        ]
        collapsed_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.4.reference-image.1.resolved-destination"
        ]
        shortcut_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.5.reference-image.1"
        ]
        shortcut_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.5.reference-image.1.reference"
        ]
        shortcut_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.5.reference-image.1.resolved-destination"
        ]
        angle_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.6.reference-image.1"
        ]
        angle_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.6.reference-image.1.resolved-destination"
        ]
        angle_image_resolved_title = reference_image_spans[
            "body.reference-images.paragraph.1.line.6.reference-image.1.resolved-title"
        ]
        angle_image_definition_destination = reference_image_spans[
            "body.reference-images.link-reference.5.destination"
        ]
        angle_image_definition_title = reference_image_spans[
            "body.reference-images.link-reference.5.title"
        ]
        escaped_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.7.reference-image.1"
        ]
        escaped_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.7.reference-image.1.reference"
        ]
        escaped_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.7.reference-image.1.resolved-destination"
        ]
        escaped_image_definition_label = reference_image_spans[
            "body.reference-images.link-reference.6.label"
        ]
        escaped_image_definition_destination = reference_image_spans[
            "body.reference-images.link-reference.6.destination"
        ]
        assert (
            public_reference_image[3]
            == private_reference_image[3]
            == outside_reference_image[3]
            == collapsed_reference_image[3]
            == shortcut_reference_image[3]
            == angle_reference_image[3]
            == escaped_reference_image[3]
            == "reference-image"
        )
        assert private_reference_alt[3] == "reference-image-alt"
        assert private_image_reference_marker[3] == "reference-image-reference"
        assert (
            private_image_resolved_destination[3]
            == "reference-image-resolved-destination"
        )
        assert private_image_resolved_title[3] == "reference-image-resolved-title"
        assert private_image_definition_destination[3] == "link-reference-destination"
        assert private_image_definition_title[3] == "link-reference-title"
        assert hidden_reference_image_code[3] == "inline-code"
        assert (
            "body.reference-images.paragraph.1.line.3.reference-image.2"
            not in reference_image_spans
        )
        assert collapsed_image_reference_marker[3] == "reference-image-reference"
        assert collapsed_image_reference_marker[2] == 0
        assert (
            collapsed_image_resolved_destination[3]
            == "reference-image-resolved-destination"
        )
        assert shortcut_image_reference_marker[3] == "reference-image-reference"
        assert shortcut_image_reference_marker[2] == 0
        assert (
            shortcut_image_resolved_destination[3]
            == "reference-image-resolved-destination"
        )
        assert angle_image_resolved_destination[3] == "reference-image-resolved-destination"
        assert angle_image_resolved_title[3] == "reference-image-resolved-title"
        assert angle_image_definition_destination[3] == "link-reference-destination"
        assert angle_image_definition_title[3] == "link-reference-title"
        assert escaped_image_reference_marker[3] == "reference-image-reference"
        assert escaped_image_resolved_destination[3] == "reference-image-resolved-destination"
        assert escaped_image_definition_label[3] == "link-reference-label"
        assert escaped_image_definition_destination[3] == "link-reference-destination"
        assert reference_image_content[
            public_reference_image[1] : public_reference_image[1]
            + public_reference_image[2]
        ] == b"![public chart][Public Image]"
        assert reference_image_content[
            private_reference_alt[1] : private_reference_alt[1]
            + private_reference_alt[2]
        ] == b"safe alt"
        assert reference_image_content[
            private_image_reference_marker[1] : private_image_reference_marker[1]
            + private_image_reference_marker[2]
        ] == b"private image"
        assert reference_image_content[
            private_image_resolved_destination[1] : private_image_resolved_destination[1]
            + private_image_resolved_destination[2]
        ] == b"https://example.test/private-image.png"
        assert reference_image_content[
            private_image_resolved_title[1] : private_image_resolved_title[1]
            + private_image_resolved_title[2]
        ] == b"Secret Image Reference Title"
        assert reference_image_content[
            private_image_definition_destination[1] : private_image_definition_destination[1]
            + private_image_definition_destination[2]
        ] == b"https://example.test/private-image.png"
        assert reference_image_content[
            private_image_definition_title[1] : private_image_definition_title[1]
            + private_image_definition_title[2]
        ] == b"Secret Image Reference Title"
        assert reference_image_content[
            hidden_reference_image_code[1] : hidden_reference_image_code[1]
            + hidden_reference_image_code[2]
        ] == b"`![hidden][private image]`"
        assert reference_image_content[
            collapsed_reference_image[1] : collapsed_reference_image[1]
            + collapsed_reference_image[2]
        ] == b"![collapsed image][]"
        assert reference_image_content[
            collapsed_image_resolved_destination[1] : collapsed_image_resolved_destination[1]
            + collapsed_image_resolved_destination[2]
        ] == b"https://example.test/collapsed-image.png"
        assert reference_image_content[
            shortcut_reference_image[1] : shortcut_reference_image[1]
            + shortcut_reference_image[2]
        ] == b"![shortcut image]"
        assert reference_image_content[
            shortcut_image_resolved_destination[1] : shortcut_image_resolved_destination[1]
            + shortcut_image_resolved_destination[2]
        ] == b"https://example.test/shortcut-image.png"
        assert reference_image_content[
            angle_image_resolved_destination[1] : angle_image_resolved_destination[1]
            + angle_image_resolved_destination[2]
        ] == b"https://example.test/angle image.png"
        assert reference_image_content[
            angle_image_definition_destination[1] : angle_image_definition_destination[1]
            + angle_image_definition_destination[2]
        ] == b"https://example.test/angle image.png"
        assert reference_image_content[
            angle_image_resolved_title[1] : angle_image_resolved_title[1]
            + angle_image_resolved_title[2]
        ] == b"Angle Image Reference Title"
        assert reference_image_content[
            angle_image_definition_title[1] : angle_image_definition_title[1]
            + angle_image_definition_title[2]
        ] == b"Angle Image Reference Title"
        assert reference_image_content[
            escaped_image_reference_marker[1] : escaped_image_reference_marker[1]
            + escaped_image_reference_marker[2]
        ] == b"escaped\\] image"
        assert reference_image_content[
            escaped_image_resolved_destination[1] : escaped_image_resolved_destination[1]
            + escaped_image_resolved_destination[2]
        ] == b"https://example.test/escaped-image.png"
        assert reference_image_content[
            escaped_image_definition_label[1] : escaped_image_definition_label[1]
            + escaped_image_definition_label[2]
        ] == b"escaped\\] image"
        assert reference_image_content[
            escaped_image_definition_destination[1] : escaped_image_definition_destination[1]
            + escaped_image_definition_destination[2]
        ] == b"https://example.test/escaped-image.png"
        fs.set_markdown_section_policy_label(
            reference_image_node,
            "body.reference-images.paragraph.1.line.2.reference-image.1.resolved-destination",
            "reference-image-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "reference-image-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                private_reference_image[1],
                len(b"![safe alt][private image]"),
            )
        ) == b"![safe alt][private image]"
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                private_image_resolved_title[1],
                len(b"Secret Image Reference Title"),
            )
        ) == b"Secret Image Reference Title"
        try:
            fs.read_node_range(
                reference_image_node,
                private_image_resolved_destination[1],
                len(b"https://example.test/private-image.png"),
            )
            assert False, "reference-image resolved destination policy should block only the definition target"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                outside_reference_image[1],
                len(b"![outside][public image]"),
            )
        ) == b"![outside][public image]"
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                collapsed_reference_image[1],
                len(b"![collapsed image][]"),
            )
        ) == b"![collapsed image][]"
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                shortcut_reference_image[1],
                len(b"![shortcut image]"),
            )
        ) == b"![shortcut image]"

        duplicate_reference_content = (
            b"# Duplicate References\n"
            b"Link [winner][dup ref] remains.\n"
            b"Image ![winner image][dup image] remains.\n"
            b"\n"
            b"[dup ref]: https://example.test/first-link\n"
            b"[DUP REF]: https://example.test/second-link\n"
            b"[dup image]: https://example.test/first-image.png\n"
            b"[DUP IMAGE]: https://example.test/second-image.png\n"
        )
        duplicate_reference_node = ws.write(
            "duplicate-reference.md", duplicate_reference_content, []
        )
        duplicate_reference_spans = {
            row[0]: row for row in fs.markdown_section_spans(duplicate_reference_node)
        }
        duplicate_link_resolved_destination = duplicate_reference_spans[
            "body.duplicate-references.paragraph.1.line.1.reference-link.1.resolved-destination"
        ]
        duplicate_image_resolved_destination = duplicate_reference_spans[
            "body.duplicate-references.paragraph.1.line.2.reference-image.1.resolved-destination"
        ]
        duplicate_first_link_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.1.destination"
        ]
        duplicate_second_link_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.2.destination"
        ]
        duplicate_first_image_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.3.destination"
        ]
        duplicate_second_image_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.4.destination"
        ]
        assert duplicate_link_resolved_destination[3] == "reference-link-resolved-destination"
        assert duplicate_image_resolved_destination[3] == "reference-image-resolved-destination"
        assert duplicate_first_link_definition_destination[3] == "link-reference-destination"
        assert duplicate_second_link_definition_destination[3] == "link-reference-destination"
        assert duplicate_first_image_definition_destination[3] == "link-reference-destination"
        assert duplicate_second_image_definition_destination[3] == "link-reference-destination"
        assert duplicate_reference_content[
            duplicate_link_resolved_destination[1] : duplicate_link_resolved_destination[1]
            + duplicate_link_resolved_destination[2]
        ] == b"https://example.test/first-link"
        assert duplicate_reference_content[
            duplicate_image_resolved_destination[1] : duplicate_image_resolved_destination[1]
            + duplicate_image_resolved_destination[2]
        ] == b"https://example.test/first-image.png"
        assert duplicate_reference_content[
            duplicate_first_link_definition_destination[1] : duplicate_first_link_definition_destination[1]
            + duplicate_first_link_definition_destination[2]
        ] == b"https://example.test/first-link"
        assert duplicate_reference_content[
            duplicate_second_link_definition_destination[1] : duplicate_second_link_definition_destination[1]
            + duplicate_second_link_definition_destination[2]
        ] == b"https://example.test/second-link"
        assert duplicate_reference_content[
            duplicate_first_image_definition_destination[1] : duplicate_first_image_definition_destination[1]
            + duplicate_first_image_definition_destination[2]
        ] == b"https://example.test/first-image.png"
        assert duplicate_reference_content[
            duplicate_second_image_definition_destination[1] : duplicate_second_image_definition_destination[1]
            + duplicate_second_image_definition_destination[2]
        ] == b"https://example.test/second-image.png"

        multiline_reference_content = (
            b"# Multiline References\n"
            b"Link [two line][multi ref] remains.\n"
            b"Image ![two line image][multi image] remains.\n"
            b"\n"
            b"[multi ref]: https://example.test/multi-reference\n"
            b"  \"Multiline Reference Title\"\n"
            b"[multi image]: https://example.test/multi-image.png\n"
            b"  'Multiline Image Title'\n"
        )
        multiline_reference_node = ws.write(
            "multiline-reference.md", multiline_reference_content, []
        )
        multiline_reference_spans = {
            row[0]: row for row in fs.markdown_section_spans(multiline_reference_node)
        }
        multiline_reference_link = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.1.reference-link.1"
        ]
        multiline_link_resolved_title = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.1.reference-link.1.resolved-title"
        ]
        multiline_link_definition = multiline_reference_spans[
            "body.multiline-references.link-reference.1"
        ]
        multiline_link_definition_destination = multiline_reference_spans[
            "body.multiline-references.link-reference.1.destination"
        ]
        multiline_link_definition_title = multiline_reference_spans[
            "body.multiline-references.link-reference.1.title"
        ]
        multiline_reference_image = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.2.reference-image.1"
        ]
        multiline_image_resolved_title = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.2.reference-image.1.resolved-title"
        ]
        multiline_image_definition = multiline_reference_spans[
            "body.multiline-references.link-reference.2"
        ]
        multiline_image_definition_destination = multiline_reference_spans[
            "body.multiline-references.link-reference.2.destination"
        ]
        multiline_image_definition_title = multiline_reference_spans[
            "body.multiline-references.link-reference.2.title"
        ]
        assert multiline_reference_link[3] == "reference-link"
        assert multiline_reference_image[3] == "reference-image"
        assert multiline_link_definition[3] == multiline_image_definition[3] == "link-reference"
        assert multiline_link_definition_destination[3] == "link-reference-destination"
        assert multiline_image_definition_destination[3] == "link-reference-destination"
        assert multiline_link_definition_title[3] == multiline_image_definition_title[3] == "link-reference-title"
        assert multiline_link_resolved_title[3] == "reference-link-resolved-title"
        assert multiline_image_resolved_title[3] == "reference-image-resolved-title"
        assert multiline_reference_content[
            multiline_link_definition[1] : multiline_link_definition[1]
            + multiline_link_definition[2]
        ] == (
            b"[multi ref]: https://example.test/multi-reference\n"
            b"  \"Multiline Reference Title\"\n"
        )
        assert multiline_reference_content[
            multiline_image_definition[1] : multiline_image_definition[1]
            + multiline_image_definition[2]
        ] == (
            b"[multi image]: https://example.test/multi-image.png\n"
            b"  'Multiline Image Title'\n"
        )
        assert multiline_reference_content[
            multiline_link_definition_destination[1] : multiline_link_definition_destination[1]
            + multiline_link_definition_destination[2]
        ] == b"https://example.test/multi-reference"
        assert multiline_reference_content[
            multiline_image_definition_destination[1] : multiline_image_definition_destination[1]
            + multiline_image_definition_destination[2]
        ] == b"https://example.test/multi-image.png"
        assert multiline_reference_content[
            multiline_link_definition_title[1] : multiline_link_definition_title[1]
            + multiline_link_definition_title[2]
        ] == b"Multiline Reference Title"
        assert multiline_reference_content[
            multiline_link_resolved_title[1] : multiline_link_resolved_title[1]
            + multiline_link_resolved_title[2]
        ] == b"Multiline Reference Title"
        assert multiline_reference_content[
            multiline_image_definition_title[1] : multiline_image_definition_title[1]
            + multiline_image_definition_title[2]
        ] == b"Multiline Image Title"
        assert multiline_reference_content[
            multiline_image_resolved_title[1] : multiline_image_resolved_title[1]
            + multiline_image_resolved_title[2]
        ] == b"Multiline Image Title"

        inline_code_content = (
            b"# Inline Code\n"
            b"Public command `ls -la` is visible.\n"
            b"Secret token `TOKEN=abc123` remains text.\n"
            b"Double tick ``code with ` inner tick`` is captured.\n"
            b"Triple tick ```code with `` inner ticks``` is captured.\n"
        )
        inline_code_node = ws.write("inline-code.md", inline_code_content, [])
        inline_code_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_code_node)
        }
        public_code = inline_code_spans["body.inline-code.paragraph.1.line.1.code.1"]
        secret_code = inline_code_spans["body.inline-code.paragraph.1.line.2.code.1"]
        double_code = inline_code_spans["body.inline-code.paragraph.1.line.3.code.1"]
        triple_code = inline_code_spans["body.inline-code.paragraph.1.line.4.code.1"]
        assert public_code[3] == secret_code[3] == double_code[3] == triple_code[3] == "inline-code"
        assert inline_code_content[public_code[1] : public_code[1] + public_code[2]] == (
            b"`ls -la`"
        )
        assert inline_code_content[secret_code[1] : secret_code[1] + secret_code[2]] == (
            b"`TOKEN=abc123`"
        )
        assert inline_code_content[double_code[1] : double_code[1] + double_code[2]] == (
            b"``code with ` inner tick``"
        )
        assert inline_code_content[triple_code[1] : triple_code[1] + triple_code[2]] == (
            b"```code with `` inner ticks```"
        )
        fs.set_markdown_section_policy_label(
            inline_code_node,
            "body.inline-code.paragraph.1.line.2.code.1",
            "inline-code-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-code-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(inline_code_node, public_code[1], len(b"`ls -la`"))
        ) == b"`ls -la`"
        assert bytes(
            fs.read_node_range(
                inline_code_node,
                inline_code_content.index(b"Secret token"),
                len(b"Secret token"),
            )
        ) == b"Secret token"
        try:
            fs.read_node_range(inline_code_node, secret_code[1], len(b"`TOKEN"))
            assert False, "markdown inline code policy should block only the code span"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_code_node,
                inline_code_content.index(b" remains text"),
                len(b" remains text"),
            )
        ) == b" remains text"

        autolink_content = (
            b"# Autolinks\n"
            b"Public URL <https://example.test/public> stays visible.\n"
            b"Secret contact <mailto:private@example.test> remains text.\n"
            b"Angle text <not-a-url> stays plain.\n"
        )
        autolink_node = ws.write("autolink.md", autolink_content, [])
        autolink_spans = {
            row[0]: row for row in fs.markdown_section_spans(autolink_node)
        }
        public_autolink = autolink_spans["body.autolinks.paragraph.1.line.1.autolink.1"]
        secret_autolink = autolink_spans["body.autolinks.paragraph.1.line.2.autolink.1"]
        public_autolink_target = autolink_spans[
            "body.autolinks.paragraph.1.line.1.autolink.1.target"
        ]
        secret_autolink_target = autolink_spans[
            "body.autolinks.paragraph.1.line.2.autolink.1.target"
        ]
        assert public_autolink[3] == secret_autolink[3] == "autolink"
        assert public_autolink_target[3] == secret_autolink_target[3] == "autolink-target"
        assert "body.autolinks.paragraph.1.line.3.autolink.1" not in autolink_spans
        assert autolink_content[
            public_autolink[1] : public_autolink[1] + public_autolink[2]
        ] == b"<https://example.test/public>"
        assert autolink_content[
            secret_autolink[1] : secret_autolink[1] + secret_autolink[2]
        ] == b"<mailto:private@example.test>"
        assert autolink_content[
            public_autolink_target[1] : public_autolink_target[1] + public_autolink_target[2]
        ] == b"https://example.test/public"
        assert autolink_content[
            secret_autolink_target[1] : secret_autolink_target[1] + secret_autolink_target[2]
        ] == b"mailto:private@example.test"
        fs.set_markdown_section_policy_label(
            autolink_node,
            "body.autolinks.paragraph.1.line.2.autolink.1",
            "autolink-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "autolink-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                autolink_node,
                public_autolink[1],
                len(b"<https://example.test/public>"),
            )
        ) == b"<https://example.test/public>"
        assert bytes(
            fs.read_node_range(
                autolink_node,
                autolink_content.index(b"Secret contact"),
                len(b"Secret contact"),
            )
        ) == b"Secret contact"
        try:
            fs.read_node_range(autolink_node, secret_autolink[1], len(b"<mailto:private"))
            assert False, "markdown autolink policy should block only the autolink"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                autolink_node,
                autolink_content.index(b" remains text"),
                len(b" remains text"),
            )
        ) == b" remains text"

        autolink_target_content = (
            b"# Autolink Targets\n"
            b"Public wrapper <https://example.test/secret-target> remains.\n"
        )
        autolink_target_node = ws.write("autolink-target.md", autolink_target_content, [])
        autolink_target_spans = {
            row[0]: row for row in fs.markdown_section_spans(autolink_target_node)
        }
        secret_autolink_target = autolink_target_spans[
            "body.autolink-targets.paragraph.1.line.1.autolink.1.target"
        ]
        assert secret_autolink_target[3] == "autolink-target"
        assert autolink_target_content[
            secret_autolink_target[1] : secret_autolink_target[1] + secret_autolink_target[2]
        ] == b"https://example.test/secret-target"
        fs.set_markdown_section_policy_label(
            autolink_target_node,
            "body.autolink-targets.paragraph.1.line.1.autolink.1.target",
            "autolink-target-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "autolink-target-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                autolink_target_node,
                autolink_target_content.index(b"Public wrapper <"),
                len(b"Public wrapper <"),
            )
        ) == b"Public wrapper <"
        try:
            fs.read_node_range(
                autolink_target_node,
                secret_autolink_target[1],
                len(b"https://example.test/secret-target"),
            )
            assert False, "markdown autolink target policy should block only the target"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                autolink_target_node,
                autolink_target_content.index(b"> remains"),
                len(b"> remains"),
            )
        ) == b"> remains"

        inline_image_content = (
            b"# Inline Images\n"
            b"Public figure ![public chart](https://example.test/public.png) remains.\n"
            b"Secret source ![safe alt](https://example.test/secret.png) remains.\n"
        )
        inline_image_node = ws.write("inline-image.md", inline_image_content, [])
        inline_image_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_image_node)
        }
        public_image = inline_image_spans[
            "body.inline-images.paragraph.1.line.1.image.1"
        ]
        secret_image = inline_image_spans[
            "body.inline-images.paragraph.1.line.2.image.1"
        ]
        public_alt = inline_image_spans[
            "body.inline-images.paragraph.1.line.1.image.1.alt"
        ]
        secret_destination = inline_image_spans[
            "body.inline-images.paragraph.1.line.2.image.1.destination"
        ]
        assert public_image[3] == secret_image[3] == "inline-image"
        assert public_alt[3] == "inline-image-alt"
        assert secret_destination[3] == "inline-image-destination"
        assert inline_image_content[
            public_image[1] : public_image[1] + public_image[2]
        ] == b"![public chart](https://example.test/public.png)"
        assert inline_image_content[
            public_alt[1] : public_alt[1] + public_alt[2]
        ] == b"public chart"
        assert inline_image_content[
            secret_destination[1] : secret_destination[1] + secret_destination[2]
        ] == b"https://example.test/secret.png"
        fs.set_markdown_section_policy_label(
            inline_image_node,
            "body.inline-images.paragraph.1.line.2.image.1.destination",
            "inline-image-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-image-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_image_node,
                inline_image_content.index(b"![safe alt]"),
                len(b"![safe alt]"),
            )
        ) == b"![safe alt]"
        try:
            fs.read_node_range(
                inline_image_node,
                secret_destination[1],
                len(b"https://example.test/secret.png"),
            )
            assert False, "markdown inline image destination policy should block only the destination"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_image_node,
                inline_image_content.index(b" remains", secret_destination[1]),
                len(b" remains"),
            )
        ) == b" remains"

        inline_image_title_content = (
            b"# Inline Image Titles\n"
            b"Public wrapper ![safe alt](https://example.test/image-title.png 'Secret Image Title') remains.\n"
            b"Paren wrapper ![safe alt](https://example.test/paren-image.png (Paren Image Title)) remains.\n"
        )
        inline_image_title_node = ws.write(
            "inline-image-title.md", inline_image_title_content, []
        )
        inline_image_title_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_image_title_node)
        }
        image_title_destination = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.1.image.1.destination"
        ]
        secret_image_title = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.1.image.1.title"
        ]
        paren_image_title_destination = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.2.image.1.destination"
        ]
        paren_image_title = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.2.image.1.title"
        ]
        assert image_title_destination[3] == paren_image_title_destination[3] == "inline-image-destination"
        assert secret_image_title[3] == paren_image_title[3] == "inline-image-title"
        assert inline_image_title_content[
            image_title_destination[1] : image_title_destination[1]
            + image_title_destination[2]
        ] == b"https://example.test/image-title.png"
        assert inline_image_title_content[
            secret_image_title[1] : secret_image_title[1] + secret_image_title[2]
        ] == b"Secret Image Title"
        assert inline_image_title_content[
            paren_image_title_destination[1] : paren_image_title_destination[1]
            + paren_image_title_destination[2]
        ] == b"https://example.test/paren-image.png"
        assert inline_image_title_content[
            paren_image_title[1] : paren_image_title[1] + paren_image_title[2]
        ] == b"Paren Image Title"
        fs.set_markdown_section_policy_label(
            inline_image_title_node,
            "body.inline-image-titles.paragraph.1.line.1.image.1.title",
            "inline-image-title-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-image-title-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_image_title_node,
                image_title_destination[1],
                len(b"https://example.test/image-title.png"),
            )
        ) == b"https://example.test/image-title.png"
        try:
            fs.read_node_range(
                inline_image_title_node,
                secret_image_title[1],
                len(b"Secret Image Title"),
            )
            assert False, "markdown inline image title policy should block only the title"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_image_title_node,
                inline_image_title_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_overlap_content = (
            b"# Inline Overlap\n"
            b"Code `[hidden](https://example.test/private)` then [public](https://example.test/public).\n"
            b"Code `<https://example.test/private>` then <https://example.test/public>.\n"
            b"Code `![hidden](https://example.test/private.png)` then ![public](https://example.test/public.png).\n"
            b"Code ``[hidden multi](https://example.test/private)`` then [public multi](https://example.test/public).\n"
        )
        inline_overlap_node = ws.write("inline-overlap.md", inline_overlap_content, [])
        inline_overlap_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_overlap_node)
        }
        hidden_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.1.code.1"
        ]
        public_link = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.1.link.1"
        ]
        hidden_url_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.2.code.1"
        ]
        public_autolink = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.2.autolink.1"
        ]
        hidden_image_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.3.code.1"
        ]
        public_image = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.3.image.1"
        ]
        hidden_multi_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.4.code.1"
        ]
        public_multi_link = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.4.link.1"
        ]
        assert hidden_code[3] == hidden_url_code[3] == "inline-code"
        assert public_link[3] == "inline-link"
        assert public_autolink[3] == "autolink"
        assert hidden_image_code[3] == "inline-code"
        assert public_image[3] == "inline-image"
        assert hidden_multi_code[3] == "inline-code"
        assert public_multi_link[3] == "inline-link"
        assert "body.inline-overlap.paragraph.1.line.1.link.2" not in inline_overlap_spans
        assert "body.inline-overlap.paragraph.1.line.2.autolink.2" not in inline_overlap_spans
        assert "body.inline-overlap.paragraph.1.line.3.image.2" not in inline_overlap_spans
        assert "body.inline-overlap.paragraph.1.line.4.link.2" not in inline_overlap_spans
        assert inline_overlap_content[
            hidden_code[1] : hidden_code[1] + hidden_code[2]
        ] == b"`[hidden](https://example.test/private)`"
        assert inline_overlap_content[
            public_link[1] : public_link[1] + public_link[2]
        ] == b"[public](https://example.test/public)"
        assert inline_overlap_content[
            hidden_multi_code[1] : hidden_multi_code[1] + hidden_multi_code[2]
        ] == b"``[hidden multi](https://example.test/private)``"
        assert inline_overlap_content[
            public_multi_link[1] : public_multi_link[1] + public_multi_link[2]
        ] == b"[public multi](https://example.test/public)"
        assert inline_overlap_content[
            hidden_url_code[1] : hidden_url_code[1] + hidden_url_code[2]
        ] == b"`<https://example.test/private>`"
        assert inline_overlap_content[
            public_autolink[1] : public_autolink[1] + public_autolink[2]
        ] == b"<https://example.test/public>"
        assert inline_overlap_content[
            hidden_image_code[1] : hidden_image_code[1] + hidden_image_code[2]
        ] == b"`![hidden](https://example.test/private.png)`"
        assert inline_overlap_content[
            public_image[1] : public_image[1] + public_image[2]
        ] == b"![public](https://example.test/public.png)"
        fs.set_markdown_section_policy_label(
            inline_overlap_node,
            "body.inline-overlap.paragraph.1.line.1.code.1",
            "inline-overlap-code-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-overlap-code-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(inline_overlap_node, hidden_code[1], len(b"`[hidden]"))
            assert False, "markdown inline code policy should block code-contained link text"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_overlap_node,
                inline_overlap_content.index(b" then [public]"),
                len(b" then "),
            )
        ) == b" then "
        assert bytes(
            fs.read_node_range(inline_overlap_node, public_link[1], len(b"[public]"))
        ) == b"[public]"

        try:
            fs.set_markdown_section_policy_label(
                node_id,
                "body.missing",
                "sensitivity",
                "restricted",
                "policy_agent",
            )
            assert False, "missing Markdown section path should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        plain_content = (
            b"Plain body without headings.\nSecond line.\n\n"
            b"* * *\n\n"
            b"- Private item\n"
        )
        plain_node = ws.write("plain.md", plain_content, [])
        plain_spans = {row[0]: row for row in fs.markdown_section_spans(plain_node)}
        plain_paragraph = plain_spans["body.paragraph.1"]
        assert plain_paragraph[3] == "paragraph"
        assert plain_content[
            plain_paragraph[1] : plain_paragraph[1] + plain_paragraph[2]
        ].startswith(b"Plain body")
        plain_thematic = plain_spans["body.thematic-break.1"]
        assert plain_thematic[3] == "thematic-break"
        assert plain_content[
            plain_thematic[1] : plain_thematic[1] + plain_thematic[2]
        ] == b"* * *\n"
        assert plain_spans["body.list.1"][3] == "list"
        fs.set_markdown_section_policy_label(
            plain_node,
            "body.paragraph.1",
            "plain-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "plain-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(plain_node, plain_paragraph[1], len(b"Plain body"))
            assert False, "markdown body paragraph policy should block the paragraph range"
        except anfs_core.PolicyDeniedError:
            pass

        setext_content = (
            b"Public Title\n"
            b"============\n"
            b"Public setext introduction.\n"
            b"\n"
            b"Private Plan\n"
            b"------------\n"
            b"Secret setext details.\n"
            b"\n"
            b"# Appendix\n"
            b"Public appendix.\n"
        )
        setext_node = ws.write("setext.md", setext_content, [])
        setext_spans = {row[0]: row for row in fs.markdown_section_spans(setext_node)}
        assert setext_spans["body.public-title"][3] == "h1"
        assert setext_spans["body.private-plan"][3] == "h2"
        assert setext_spans["body.appendix"][3] == "h1"
        assert "body.public-title.thematic-break.1" not in setext_spans
        assert "body.private-plan.thematic-break.1" not in setext_spans
        assert "body.public-title.paragraph.1" in setext_spans
        assert "body.private-plan.paragraph.1" in setext_spans
        private_plan = setext_spans["body.private-plan"]
        assert setext_content[
            private_plan[1] : private_plan[1] + private_plan[2]
        ].startswith(b"Private Plan\n------------\n")
        assert b"# Appendix" not in setext_content[
            private_plan[1] : private_plan[1] + private_plan[2]
        ]
        fs.set_markdown_section_policy_label(
            setext_node,
            "body.private-plan",
            "setext-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "setext-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(setext_node, private_plan[1], len(b"Private Plan"))
            assert False, "setext section policy should block the section range"
        except anfs_core.PolicyDeniedError:
            pass

        list_item_content = (
            b"# Tasks\n"
            b"- Public task\n"
            b"- Secret task\n"
            b"- Public follow-up\n"
        )
        list_item_node = ws.write("list-items.md", list_item_content, [])
        list_item_spans = {
            row[0]: row for row in fs.markdown_section_spans(list_item_node)
        }
        first_item = list_item_spans["body.tasks.list.1.item.1"]
        second_item = list_item_spans["body.tasks.list.1.item.2"]
        third_item = list_item_spans["body.tasks.list.1.item.3"]
        assert first_item[3] == second_item[3] == third_item[3] == "list-item"
        assert list_item_content[
            second_item[1] : second_item[1] + second_item[2]
        ] == b"- Secret task\n"
        fs.set_markdown_section_policy_label(
            list_item_node,
            "body.tasks.list.1.item.2",
            "list-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "list-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(list_item_node, first_item[1], len(b"- Public task"))
        ) == b"- Public task"
        try:
            fs.read_node_range(list_item_node, second_item[1], len(b"- Secret task"))
            assert False, "markdown list item policy should block only the labeled item"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(list_item_node, third_item[1], len(b"- Public follow-up"))
        ) == b"- Public follow-up"

        task_list_content = (
            b"# Checklist\n"
            b"- [ ] Public todo\n"
            b"- [x] Secret decision\n"
            b"1. [X] Public done\n"
        )
        task_list_node = ws.write("task-list.md", task_list_content, [])
        task_list_spans = {
            row[0]: row for row in fs.markdown_section_spans(task_list_node)
        }
        first_task_item = task_list_spans["body.checklist.list.1.item.1"]
        secret_task_item = task_list_spans["body.checklist.list.1.item.2"]
        third_task_item = task_list_spans["body.checklist.list.1.item.3"]
        first_checkbox = task_list_spans["body.checklist.list.1.item.1.checkbox"]
        secret_checkbox = task_list_spans["body.checklist.list.1.item.2.checkbox"]
        third_checkbox = task_list_spans["body.checklist.list.1.item.3.checkbox"]
        assert (
            first_task_item[3]
            == secret_task_item[3]
            == third_task_item[3]
            == "list-item"
        )
        assert (
            first_checkbox[3]
            == secret_checkbox[3]
            == third_checkbox[3]
            == "task-checkbox"
        )
        assert task_list_content[
            first_checkbox[1] : first_checkbox[1] + first_checkbox[2]
        ] == b"[ ]"
        assert task_list_content[
            secret_checkbox[1] : secret_checkbox[1] + secret_checkbox[2]
        ] == b"[x]"
        assert task_list_content[
            third_checkbox[1] : third_checkbox[1] + third_checkbox[2]
        ] == b"[X]"
        fs.set_markdown_section_policy_label(
            task_list_node,
            "body.checklist.list.1.item.2.checkbox",
            "task-state-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "task-state-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(task_list_node, first_checkbox[1], len(b"[ ]"))
        ) == b"[ ]"
        try:
            fs.read_node_range(task_list_node, secret_checkbox[1], len(b"[x]"))
            assert False, "markdown task checkbox policy should block only the checkbox"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                task_list_node,
                task_list_content.index(b"Secret decision"),
                len(b"Secret decision"),
            )
        ) == b"Secret decision"
        assert bytes(
            fs.read_node_range(task_list_node, third_checkbox[1], len(b"[X]"))
        ) == b"[X]"

        table_row_content = (
            b"# Accounts\n"
            b"| Name | Status |\n"
            b"| --- | --- |\n"
            b"| Ada | Public |\n"
            b"| Byron | Secret |\n"
        )
        table_row_node = ws.write("table-rows.md", table_row_content, [])
        table_row_spans = {
            row[0]: row for row in fs.markdown_section_spans(table_row_node)
        }
        header_row = table_row_spans["body.accounts.table.1.row.1"]
        public_row = table_row_spans["body.accounts.table.1.row.2"]
        secret_row = table_row_spans["body.accounts.table.1.row.3"]
        assert header_row[3] == public_row[3] == secret_row[3] == "table-row"
        name_align = table_row_spans["body.accounts.table.1.align.1"]
        status_align = table_row_spans["body.accounts.table.1.align.2"]
        assert name_align[3] == status_align[3] == "table-align"
        assert table_row_content[
            header_row[1] : header_row[1] + header_row[2]
        ] == b"| Name | Status |\n"
        assert table_row_content[
            public_row[1] : public_row[1] + public_row[2]
        ] == b"| Ada | Public |\n"
        assert table_row_content[
            secret_row[1] : secret_row[1] + secret_row[2]
        ] == b"| Byron | Secret |\n"
        assert table_row_content[
            name_align[1] : name_align[1] + name_align[2]
        ] == b"---"
        assert table_row_content[
            status_align[1] : status_align[1] + status_align[2]
        ] == b"---"
        header_name_cell = table_row_spans["body.accounts.table.1.row.1.cell.1"]
        header_status_cell = table_row_spans["body.accounts.table.1.row.1.cell.2"]
        public_name_cell = table_row_spans["body.accounts.table.1.row.2.cell.1"]
        secret_status_cell = table_row_spans["body.accounts.table.1.row.3.cell.2"]
        assert (
            header_name_cell[3]
            == header_status_cell[3]
            == public_name_cell[3]
            == secret_status_cell[3]
            == "table-cell"
        )
        assert table_row_content[
            header_name_cell[1] : header_name_cell[1] + header_name_cell[2]
        ] == b"Name"
        assert table_row_content[
            header_status_cell[1] : header_status_cell[1] + header_status_cell[2]
        ] == b"Status"
        assert table_row_content[
            public_name_cell[1] : public_name_cell[1] + public_name_cell[2]
        ] == b"Ada"
        assert table_row_content[
            secret_status_cell[1] : secret_status_cell[1] + secret_status_cell[2]
        ] == b"Secret"
        assert "body.accounts.table.1.row.4" not in table_row_spans
        fs.set_markdown_section_policy_label(
            table_row_node,
            "body.accounts.table.1.row.3",
            "table-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "table-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(table_row_node, header_row[1], len(b"| Name | Status |"))
        ) == b"| Name | Status |"
        assert bytes(
            fs.read_node_range(table_row_node, public_row[1], len(b"| Ada | Public |"))
        ) == b"| Ada | Public |"
        try:
            fs.read_node_range(table_row_node, secret_row[1], len(b"| Byron | Secret |"))
            assert False, "markdown table row policy should block only the labeled row"
        except anfs_core.PolicyDeniedError:
            pass

        table_cell_content = (
            b"# Table Cells\n"
            b"| Name | Status |\n"
            b"| --- | --- |\n"
            b"| Ada | Public |\n"
            b"| Byron | Secret |\n"
        )
        table_cell_node = ws.write("table-cells.md", table_cell_content, [])
        table_cell_spans = {
            row[0]: row for row in fs.markdown_section_spans(table_cell_node)
        }
        public_cell = table_cell_spans["body.table-cells.table.1.row.3.cell.1"]
        secret_cell = table_cell_spans["body.table-cells.table.1.row.3.cell.2"]
        assert public_cell[3] == secret_cell[3] == "table-cell"
        fs.set_markdown_section_policy_label(
            table_cell_node,
            "body.table-cells.table.1.row.3.cell.2",
            "table-cell-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "table-cell-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(table_cell_node, public_cell[1], len(b"Byron"))
        ) == b"Byron"
        try:
            fs.read_node_range(table_cell_node, secret_cell[1], len(b"Secret"))
            assert False, "markdown table cell policy should block only the labeled cell"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                table_cell_node,
                table_cell_content.index(b"| Ada | Public |"),
                len(b"| Ada | Public |"),
            )
        ) == b"| Ada | Public |"

        escaped_table_content = (
            b"# Escaped Table\n"
            b"| Name | Status |\n"
            b"| --- | --- |\n"
            b"| Plan A\\|B | Secret |\n"
        )
        escaped_table_node = ws.write("escaped-table.md", escaped_table_content, [])
        escaped_table_spans = {
            row[0]: row for row in fs.markdown_section_spans(escaped_table_node)
        }
        escaped_name_cell = escaped_table_spans[
            "body.escaped-table.table.1.row.2.cell.1"
        ]
        escaped_secret_cell = escaped_table_spans[
            "body.escaped-table.table.1.row.2.cell.2"
        ]
        assert (
            escaped_table_content[
                escaped_name_cell[1] : escaped_name_cell[1] + escaped_name_cell[2]
            ]
            == b"Plan A\\|B"
        )
        assert (
            escaped_table_content[
                escaped_secret_cell[1] : escaped_secret_cell[1]
                + escaped_secret_cell[2]
            ]
            == b"Secret"
        )
        assert "body.escaped-table.table.1.row.2.cell.3" not in escaped_table_spans
        fs.set_markdown_section_policy_label(
            escaped_table_node,
            "body.escaped-table.table.1.row.2.cell.2",
            "escaped-table-cell-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "escaped-table-cell-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                escaped_table_node, escaped_name_cell[1], len(b"Plan A\\|B")
            )
        ) == b"Plan A\\|B"
        try:
            fs.read_node_range(
                escaped_table_node, escaped_secret_cell[1], len(b"Secret")
            )
            assert False, "escaped pipe should not split an extra table cell"
        except anfs_core.PolicyDeniedError:
            pass

        align_table_content = (
            b"# Align Table\n"
            b"| Name | Amount | Notes |\n"
            b"| :--- | ---: | :---: |\n"
            b"| Ada | 10 | Public |\n"
        )
        align_table_node = ws.write("align-table.md", align_table_content, [])
        align_table_spans = {
            row[0]: row for row in fs.markdown_section_spans(align_table_node)
        }
        left_align = align_table_spans["body.align-table.table.1.align.1"]
        right_align = align_table_spans["body.align-table.table.1.align.2"]
        center_align = align_table_spans["body.align-table.table.1.align.3"]
        assert left_align[3] == right_align[3] == center_align[3] == "table-align"
        assert align_table_content[
            left_align[1] : left_align[1] + left_align[2]
        ] == b":---"
        assert align_table_content[
            right_align[1] : right_align[1] + right_align[2]
        ] == b"---:"
        assert align_table_content[
            center_align[1] : center_align[1] + center_align[2]
        ] == b":---:"
        fs.set_markdown_section_policy_label(
            align_table_node,
            "body.align-table.table.1.align.2",
            "table-align-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "table-align-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                align_table_node,
                align_table_content.index(b"| Ada | 10 | Public |"),
                len(b"| Ada | 10 | Public |"),
            )
        ) == b"| Ada | 10 | Public |"
        try:
            fs.read_node_range(align_table_node, right_align[1], len(b"---:"))
            assert False, "markdown table alignment policy should block only the labeled alignment"
        except anfs_core.PolicyDeniedError:
            pass

        html_content = (
            b"# Html\n"
            b"Public html intro.\n"
            b"<DIV class=\"secret\">\n"
            b"# Not A Heading\n"
            b"Secret HTML panel\n"
            b"</DIV>\n"
            b"\n"
            b"After html.\n"
        )
        html_node = ws.write("html-block.md", html_content, [])
        html_spans = {row[0]: row for row in fs.markdown_section_spans(html_node)}
        html_block = html_spans["body.html.html.1"]
        assert html_block[3] == "html"
        assert "body.not-a-heading" not in html_spans
        assert html_content[html_block[1] : html_block[1] + html_block[2]] == (
            b"<DIV class=\"secret\">\n# Not A Heading\nSecret HTML panel\n</DIV>\n"
        )
        assert html_spans["body.html.paragraph.1"][3] == "paragraph"
        assert html_spans["body.html.paragraph.2"][3] == "paragraph"
        fs.set_markdown_section_policy_label(
            html_node,
            "body.html.html.1",
            "html-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "html-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                html_node,
                html_content.index(b"Public html intro"),
                len(b"Public html intro"),
            )
        ) == b"Public html intro"
        try:
            fs.read_node_range(html_node, html_block[1], len(b"<DIV"))
            assert False, "markdown HTML block policy should block only the HTML block"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                html_node,
                html_content.index(b"After html"),
                len(b"After html"),
            )
        ) == b"After html"

        indented_code_content = (
            b"# Code\n"
            b"Public code intro.\n"
            b"\n"
            b"    # Not A Heading\n"
            b"    secret_call()\n"
            b"\n"
            b"After code.\n"
        )
        indented_code_node = ws.write("indented-code.md", indented_code_content, [])
        indented_code_spans = {
            row[0]: row for row in fs.markdown_section_spans(indented_code_node)
        }
        indented_code_block = indented_code_spans["body.code.code.1"]
        assert indented_code_block[3] == "code"
        assert "body.not-a-heading" not in indented_code_spans
        assert indented_code_content[
            indented_code_block[1] : indented_code_block[1] + indented_code_block[2]
        ] == b"    # Not A Heading\n    secret_call()\n"
        assert indented_code_spans["body.code.paragraph.1"][3] == "paragraph"
        assert indented_code_spans["body.code.paragraph.2"][3] == "paragraph"
        fs.set_markdown_section_policy_label(
            indented_code_node,
            "body.code.code.1",
            "indented-code-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "indented-code-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                indented_code_node,
                indented_code_content.index(b"Public code intro"),
                len(b"Public code intro"),
            )
        ) == b"Public code intro"
        try:
            fs.read_node_range(indented_code_node, indented_code_block[1], len(b"    #"))
            assert False, "markdown indented code policy should block only the code block"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                indented_code_node,
                indented_code_content.index(b"After code"),
                len(b"After code"),
            )
        ) == b"After code"

        link_ref_content = (
            b"# References\n"
            b"Public reference intro.\n"
            b"\n"
            b"[secret-ref]: https://example.test/private \"Secret\"\n"
            b"\n"
            b"After reference.\n"
            b"\n"
            b"[not-a-heading]: https://example.test/public\n"
            b"---\n"
        )
        link_ref_node = ws.write("link-reference.md", link_ref_content, [])
        link_ref_spans = {
            row[0]: row for row in fs.markdown_section_spans(link_ref_node)
        }
        link_ref_block = link_ref_spans["body.references.link-reference.1"]
        second_link_ref_block = link_ref_spans["body.references.link-reference.2"]
        assert link_ref_block[3] == second_link_ref_block[3] == "link-reference"
        assert "body.not-a-heading" not in link_ref_spans
        assert link_ref_content[
            link_ref_block[1] : link_ref_block[1] + link_ref_block[2]
        ] == b"[secret-ref]: https://example.test/private \"Secret\"\n"
        assert link_ref_content[
            second_link_ref_block[1] : second_link_ref_block[1] + second_link_ref_block[2]
        ] == b"[not-a-heading]: https://example.test/public\n"
        assert link_ref_spans["body.references.paragraph.1"][3] == "paragraph"
        assert link_ref_spans["body.references.paragraph.2"][3] == "paragraph"
        fs.set_markdown_section_policy_label(
            link_ref_node,
            "body.references.link-reference.1",
            "link-reference-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "link-reference-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                link_ref_node,
                link_ref_content.index(b"Public reference intro"),
                len(b"Public reference intro"),
            )
        ) == b"Public reference intro"
        try:
            fs.read_node_range(link_ref_node, link_ref_block[1], len(b"[secret-ref]"))
            assert False, "markdown link reference policy should block only the reference"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                link_ref_node,
                link_ref_content.index(b"After reference"),
                len(b"After reference"),
            )
        ) == b"After reference"
        assert bytes(
            fs.read_node_range(
                link_ref_node,
                second_link_ref_block[1],
                len(b"[not-a-heading]"),
            )
        ) == b"[not-a-heading]"

        blockquote_content = (
            b"# Quotes\n"
            b"Public quote intro.\n"
            b"\n"
            b"> Public quoted line\n"
            b"> Secret quoted line\n"
            b"> Public quoted follow-up\n"
            b"\n"
            b"After quote.\n"
        )
        blockquote_node = ws.write("blockquote.md", blockquote_content, [])
        blockquote_spans = {
            row[0]: row for row in fs.markdown_section_spans(blockquote_node)
        }
        blockquote_block = blockquote_spans["body.quotes.blockquote.1"]
        first_quote_line = blockquote_spans["body.quotes.blockquote.1.line.1"]
        secret_quote_line = blockquote_spans["body.quotes.blockquote.1.line.2"]
        third_quote_line = blockquote_spans["body.quotes.blockquote.1.line.3"]
        assert blockquote_block[3] == "blockquote"
        assert (
            first_quote_line[3]
            == secret_quote_line[3]
            == third_quote_line[3]
            == "blockquote-line"
        )
        assert blockquote_content[
            secret_quote_line[1] : secret_quote_line[1] + secret_quote_line[2]
        ] == b"> Secret quoted line\n"
        fs.set_markdown_section_policy_label(
            blockquote_node,
            "body.quotes.blockquote.1.line.2",
            "quote-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "quote-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                blockquote_content.index(b"Public quote intro"),
                len(b"Public quote intro"),
            )
        ) == b"Public quote intro"
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                first_quote_line[1],
                len(b"> Public quoted line"),
            )
        ) == b"> Public quoted line"
        try:
            fs.read_node_range(blockquote_node, secret_quote_line[1], len(b"> Secret"))
            assert False, "markdown blockquote line policy should block only the line"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                third_quote_line[1],
                len(b"> Public quoted follow-up"),
            )
        ) == b"> Public quoted follow-up"
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                blockquote_content.index(b"After quote"),
                len(b"After quote"),
            )
        ) == b"After quote"
        assert fs.verify_integrity() == []
