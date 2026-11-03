import test from "node:test";
import assert from "node:assert/strict";
import {parseGradebook, StudentContractError} from "../src/features/student/model.ts";

const publication = () => ({publication_id:"gbook_test",title:"학기 성적",published_at:"2026-11-03T00:00:00Z",version:2,
  items:[{key:"mid",label:"중간고사",value:0},{key:"final",label:"기말고사",value:null},{key:"attendance",label:"출석",value:9.5},{key:"mark",label:"평가",value:"A+"}]});

test("spreadsheet results retain custom columns, zeros, blanks and text without inventing totals", () => {
  const result = parseGradebook(publication())!;
  assert.deepEqual(result.items.map(row => row.value), [0, null, 9.5, "A+"]);
  assert.equal(result.version, 2);
  assert.equal(parseGradebook(null), null);
  assert.equal(parseGradebook(undefined), null);
});

test("malformed or ambiguous gradebook cells cannot become a visible result", () => {
  for (const value of [NaN, Infinity, true, {}, [], undefined]) {
    const changed = publication();
    assert.throws(() => parseGradebook({...changed,items:[{...changed.items[0],value}]}), StudentContractError);
  }
  for (const patch of [{version:0},{version:1.5},{publication_id:"../grades"},{published_at:"not a date"},{items:[]},
    {items:[publication().items[0],publication().items[0]]},{items:[{key:"one",label:"",value:0}]}]) {
    assert.throws(() => parseGradebook({...publication(),...patch}), StudentContractError);
  }
});
