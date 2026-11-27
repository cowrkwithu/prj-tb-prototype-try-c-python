"""JSONField 목록의 '하나라도 포함' 조회 — `Q(comorbidities__has_any_value=["당뇨", "HIV"])`.

DB 안에서 평가하므로 SQL 파라미터 수 = 선택한 값 개수(환자 수와 무관).
  SQLite     : json_type(col) = 'array' AND
               EXISTS (SELECT 1 FROM json_each(col) AS je WHERE je.type = 'text' AND je.value IN (%s, …))
  PostgreSQL : jsonb_typeof(col) = 'array' AND col ?| %s::text[]
값이 배열일 때만 원소를 본다(스칼라 문자열 "당뇨" 같은 비정상 값은 일치시키지 않음).
그 밖의 DB 는 지원하지 않는다(NotSupportedError) — 이 포털은 SQLite·PostgreSQL 만 대상으로 한다.
"""
from django.db import NotSupportedError
from django.db.models import JSONField, Lookup


class HasAnyValue(Lookup):
    lookup_name = "has_any_value"
    prepare_rhs = False

    def get_prep_lookup(self):
        values = self.rhs
        if not isinstance(values, (list, tuple)) or not values or not all(isinstance(v, str) for v in values):
            raise ValueError("has_any_value 에는 비어 있지 않은 문자열 목록이 필요합니다.")
        return list(dict.fromkeys(values))

    def as_sql(self, compiler, connection):
        raise NotSupportedError(f"has_any_value 는 {connection.vendor} DB 를 지원하지 않습니다.")

    def as_sqlite(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        placeholders = ", ".join(["%s"] * len(self.rhs))
        sql = (f"(json_type({lhs}) = 'array' AND EXISTS (SELECT 1 FROM json_each({lhs}) AS je "
               f"WHERE je.type = 'text' AND je.value IN ({placeholders})))")
        return sql, (*lhs_params, *lhs_params, *self.rhs)

    def as_postgresql(self, compiler, connection):
        lhs, lhs_params = self.process_lhs(compiler, connection)
        return (f"(jsonb_typeof({lhs}) = 'array' AND {lhs} ?| %s::text[])",
                (*lhs_params, *lhs_params, list(self.rhs)))


def register():
    JSONField.register_lookup(HasAnyValue)
