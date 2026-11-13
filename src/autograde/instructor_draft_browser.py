"""Draft-only in-memory saves; included in the instructor CSP-hashed script."""

SCRIPT = r"""
  const inlineForms = guarded.filter(form => form.hasAttribute('data-inline-draft-save'));
  if (inlineForms.length) {
    const overview = document.querySelector('[data-draft-save-overview]');
    const announce = text => { if (overview) overview.textContent = text; };
    const value = (form, name) => [...form.elements].find(el => el.name === name);
    const scope = form => form.getAttribute('action').replace(/\/(?:uploads\/(?:starter|solution|negative)|starter-template|grading-template)$/, '');
    const setValues = (form, values) => {
      [...form.elements].forEach(el => {
        if (!Object.hasOwn(values, el.name) || ['hidden', 'file', 'submit', 'button'].includes(el.type)) return;
        if (el.type === 'checkbox') el.checked = values[el.name] === true;
        else el.value = String(values[el.name] ?? '');
      });
      const editor = form.querySelector('[data-case-editor]');
      if (editor) editor.dispatchEvent(new Event('autograde:server-values'));
    };
    const clean = form => { form.removeAttribute('data-recovered'); baselines.set(form, fingerprint(form)); };
    let needsReview = false;
    let uncertain = null;
    const recovery = document.querySelector('[data-draft-recovery]');
    const recoverButton = document.querySelector('[data-draft-recover]');
    const conflict = document.querySelector('[data-draft-conflict]');
    const serverValues = document.querySelector('[data-draft-server-values]');
    const validReceipt = (receipt, path, previous) => receipt?.ok === true && receipt.path === path &&
      receipt.previous_revision === previous && receipt.revision === previous + 1 &&
      Array.isArray(receipt.summary) && receipt.summary.length === 5 && receipt.values && receipt.uploads && typeof receipt.can_check === 'boolean';
    const reviewNeeded = (attempt, message) => {
      uncertain = attempt; needsReview = true;
      if (recovery) recovery.hidden = false;
      if (conflict) conflict.hidden = true;
      showStatus(attempt.form, message + ' 입력·파일 선택은 유지했습니다. 위의 ‘저장 상태 다시 확인’을 누르세요.');
      announce('저장 여부 확인 필요 · 자동 재전송하지 않습니다.');
    };
    const compareServer = receipt => {
      if (!conflict || !serverValues) return;
      const labels = {title: '과제 제목', description: '문제 설명', language: '언어', platform: '개발 환경',
        result_policy: '점수 공개 시점', opens_at: '수령 시작', due_at: '제출 마감', no_deadline: '마감 없음', negative_score: '오답 예상 점수'};
      const testLabels = {title: '이름', input: '입력', output: '출력', weight: '배점', evaluation: '평가 요소', hint: '힌트', public: '공개 여부'};
      const label = key => {
        const test = /^test_(\d+)_(.+)$/.exec(key);
        return test ? `테스트 ${Number(test[1]) + 1} · ${testLabels[test[2]] || test[2]}` : labels[key] || key;
      };
      serverValues.textContent = `서버 저장 버전 ${receipt.revision} · ${receipt.updated_at} KST\n\n` +
        Object.entries(receipt.values).filter(([, v]) => v !== '').map(([k, v]) => `${label(k)}: ${v}`).join('\n') +
        '\n\n등록 파일: ' + Object.entries(receipt.uploads).map(([role, upload]) => `${role}: ${upload.files.join(', ')}`).join(' / ');
      conflict.hidden = false; conflict.open = true;
    };
    const renderReceipt = receipt => {
      const setText = (selector, text) => {
        const el = document.querySelector(selector); if (el) el.textContent = text;
      };
      setText('[data-draft-title]', receipt.title);
      setText('[data-draft-language]', receipt.language);
      setText('[data-draft-revision]', String(receipt.revision));
      setText('[data-draft-updated]', receipt.updated_at);
      document.querySelectorAll('[data-draft-summary-value]').forEach((el, index) => { el.textContent = receipt.summary[index]; });
      document.querySelectorAll('[data-upload-state]').forEach(el => {
        const upload = receipt.uploads[el.dataset.uploadState];
        el.textContent = upload ? `서버 등록됨 · ${upload.size_bytes} bytes · ${upload.files.join(', ')}` : '아직 등록된 파일이 없습니다.';
      });
      setText('[data-grading-receipt]', receipt.grading_status);
      setText('[data-draft-check-status]', `현재 저장 버전 ${receipt.revision}은 아직 검증하지 않았습니다. 아래 이전 결과와 구분하여 새 검증을 시작하세요.`);
      setText('#missing-validation-materials', receipt.can_check ? '필수 자료 서버 저장 완료 · 검증을 시작할 수 있습니다.' : receipt.grading_status);
      document.querySelectorAll('form').forEach(form => {
        const action = form.getAttribute('action');
        if (action === receipt.path + '/checks') {
          form.querySelectorAll('button[type=submit],button:not([type])').forEach(button => { button.disabled = !receipt.can_check; });
        }
      });
      // Any edit invalidates the previous check. Never retain an old publish
      // confirmation, and never automatically enqueue validation or publication.
      setText('#draft-publish', '5. 학생 공개 · 자료가 변경되었습니다. 현재 저장 버전을 검증한 뒤 공개할 수 있습니다.');
    };
    const applyReceipt = (attempt, receipt) => {
      const {form, before, previous, path, importing, tests, fileFields, chosen} = attempt;
      const unchanged = fingerprint(form) === before && fileFields.every(el => el.files === chosen.get(el));
      const testsConflict = importing && tests && dirty(tests);
      document.querySelectorAll('form').forEach(candidate => {
        const action = candidate.getAttribute('action') || '';
        if (!(action === path || action.startsWith(path + '/')) || (testsConflict && candidate === tests)) return;
        ['revision', 'delete_revision'].forEach(name => {
          const field = value(candidate, name);
          if (field && field.value === String(previous)) field.value = String(receipt.revision);
        });
      });
      if (unchanged) {
        if (!importing) setValues(form, receipt.values);
        fileFields.forEach(el => { if (el.files === chosen.get(el)) el.value = ''; });
        clean(form);
      } else form.setAttribute('data-recovered', '');
      if (importing && tests) {
        if (testsConflict) {
          tests.setAttribute('data-save-conflict', '');
          showStatus(tests, '채점 템플릿 저장 중 테스트 입력이 변경되었습니다. 입력을 유지했습니다. 최신 서버 테스트와 새 탭에서 비교한 뒤 반영하세요.');
        } else { setValues(tests, receipt.values); clean(tests); showStatus(tests, '채점 템플릿의 테스트·배점이 서버에 저장되었습니다.'); }
      }
      renderReceipt(receipt);
      const suffix = unchanged ? '이 영역의 내용이 서버에 저장되었습니다.' : '전송한 내용은 저장되었고, 저장 중 새로 입력한 내용은 아직 미저장입니다.';
      showStatus(form, `저장 완료 · 버전 ${receipt.revision} · ${suffix}`);
      announce(`서버 저장 완료 · 버전 ${receipt.revision} · ` + (guarded.some(dirty) ? '다른 미저장 입력이 남아 있습니다.' : '모든 입력이 서버 저장 내용과 일치합니다.'));
    };
    if (recoverButton) recoverButton.addEventListener('click', async () => {
      if (!uncertain || draftSavePending) return;
      const attempt = uncertain;
      const {form, path, previous, requestKey, kind} = attempt;
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 15000);
      draftSavePending = true; recoverButton.disabled = true;
      showStatus(form, '서버 저장 기록 확인 중 · 입력은 다시 전송하지 않습니다.');
      try {
        const query = new URLSearchParams({revision: String(previous), save_request: requestKey, save_kind: kind});
        const response = await fetch(path + '/save-state?' + query, {method: 'GET', credentials: 'same-origin', cache: 'no-store',
          redirect: 'error', headers: {'Accept': 'application/json'}, signal: controller.signal});
        if (!response.ok || !(response.headers.get('Content-Type') || '').toLowerCase().includes('application/json')) throw Error('unconfirmed');
        const state = await response.json();
        const receipt = state?.receipt;
        if (state.ok !== true || state.path !== path || state.save_request !== requestKey || state.save_kind !== kind || state.previous_revision !== previous ||
            !receipt || !Number.isSafeInteger(receipt.revision) || !validReceipt(receipt, path, receipt.revision - 1)) throw Error('unconfirmed');
        if (state.status === 'saved' && state.request_saved === true && receipt.revision === previous + 1) {
          applyReceipt(attempt, receipt);
          needsReview = false; uncertain = null;
          if (recovery) recovery.hidden = true;
          announce(`서버 저장 기록 확인 완료 · 버전 ${receipt.revision} · ` + (guarded.some(dirty) ? '미저장 입력은 그대로 유지했습니다.' : '이어서 작성할 수 있습니다.'));
        } else if (state.status === 'not_saved' && state.request_saved === false && receipt.revision === previous) {
          needsReview = false; uncertain = null;
          if (recovery) recovery.hidden = true;
          showStatus(form, '서버에 저장되지 않은 상태를 확인했습니다. 입력·파일 선택은 유지했습니다. 이 영역의 저장 버튼으로 다시 저장하세요.');
          announce('저장 다시 시도 가능 · 자동으로 전송하지 않았습니다.');
        } else if (['conflict', 'locked'].includes(state.status)) {
          compareServer(receipt);
          showStatus(form, (state.request_saved ? '전송한 내용의 저장은 확인했지만 이후 서버 상태가 변경되었습니다. ' : '다른 창의 변경 또는 편집 잠금이 확인되었습니다. ') +
            `현재 서버 버전 ${receipt.revision} · 아래 서버 내용과 현재 입력을 비교하세요. 현재 입력과 이전 버전을 유지하며 자동으로 덮어쓰지 않습니다.`);
          announce('비교 필요 · 입력은 유지했습니다. 최신 초안을 새 탭에서 열어 작업을 이어갈 수 있습니다.');
        } else throw Error('unconfirmed');
      } catch (_) {
        showStatus(form, '서버 저장 상태를 아직 확인할 수 없습니다. 연결·로그인 상태를 확인한 뒤 ‘저장 상태 다시 확인’을 다시 누르세요. 입력·파일 선택은 유지했습니다.');
        announce('확인 실패 · 현재 입력 보존 · 다시 확인할 수 있습니다.');
      } finally {
        clearTimeout(timeout); draftSavePending = false; recoverButton.disabled = false;
      }
    });
    document.addEventListener('submit', event => {
      if (draftSavePending) {
        event.preventDefault(); announce('저장 결과를 확인하고 있습니다. 완료 후 다음 작업을 진행하세요.'); return;
      }
      const form = event.target;
      if (!inlineForms.includes(form) || event.defaultPrevented) return;
      event.preventDefault();
      if (needsReview || form.hasAttribute('data-save-conflict')) {
        showStatus(form, '저장 상태 다시 확인으로 먼저 서버 기록을 확인하세요. 비교가 필요한 입력은 최신 초안과 비교해 주세요. 현재 입력은 유지되며 자동으로 다시 전송하지 않습니다.'); return;
      }
      const action = form.getAttribute('action');
      const path = scope(form);
      const tests = inlineForms.find(candidate => scope(candidate) === path && candidate.dataset.formKind === 'tests');
      const problem = inlineForms.find(candidate => scope(candidate) === path && candidate.dataset.formKind === 'problem');
      const importing = action.endsWith('/grading-template');
      if (action.endsWith('/starter-template') && problem && dirty(problem)) {
        showStatus(form, '문제·일정의 미저장 입력을 먼저 저장하세요. 기본 템플릿의 README는 서버에 저장된 문제 설명으로 만듭니다.'); return;
      }
      if (importing && tests && dirty(tests)) {
        showStatus(form, '테스트·배점에 미저장 변경이 있습니다. 그 내용을 먼저 저장한 뒤 채점 템플릿으로 교체할지 확인하세요. 선택한 ZIP은 유지됩니다.'); return;
      }
      const before = fingerprint(form);
      const previous = Number(value(form, 'revision')?.value);
      if (!Number.isSafeInteger(previous) || previous < 1 || new URL(action, window.location.href).origin !== window.location.origin) {
        showStatus(form, '저장 주소 또는 버전을 확인할 수 없습니다. 최신 초안을 새 탭에서 확인하세요.'); return;
      }
      const data = new FormData(form);
      data.set('inline_save', 'yes');
      const requestKey = [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
      data.set('save_request', requestKey);
      const fileFields = [...form.querySelectorAll('input[type=file]')];
      const chosen = new Map(fileFields.map(el => [el, el.files]));
      const kind = action === path ? (form.dataset.formKind === 'tests' ? 'tests' : 'problem') :
        action.endsWith('/starter-template') ? 'uploads/starter' : action.slice(path.length + 1);
      const attempt = {form, before, previous, path, importing, tests, fileFields, chosen, requestKey, kind};
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 60000);
      draftSavePending = true;
      showStatus(form, uploadForm(form) ? '전송·서버 저장 확인 중 · 아직 저장 완료가 아닙니다. 코드 실행 검증은 저장 후 별도로 시작합니다.' : '서버에 저장 중 · 다른 영역의 입력은 유지됩니다.');
      announce('현재 영역을 저장하고 있습니다. 다른 영역은 계속 작성할 수 있습니다.');
      (async () => {
        try {
          const response = await fetch(action, {method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
            headers: {'Accept': 'application/json'}, body: uploadForm(form) ? data : new URLSearchParams(data), signal: controller.signal});
          const isJSON = (response.headers.get('Content-Type') || '').toLowerCase().includes('application/json');
          if (!isJSON) throw new Error('unconfirmed');
          const receipt = await response.json();
          if (!response.ok) {
            if (response.status === 400 && receipt?.error?.code === 'draft_input') {
              showStatus(form, `저장되지 않았습니다 · ${receipt.error.message} 입력·파일 선택은 유지했습니다.`);
              announce('입력을 수정한 뒤 이 영역만 다시 저장하세요.');
            } else reviewNeeded(attempt, '저장 결과 또는 권한을 확인해야 합니다.');
            return;
          }
          if (!validReceipt(receipt, path, previous)) throw new Error('unconfirmed');
          applyReceipt(attempt, receipt);
        } catch (_) {
          reviewNeeded(attempt, '저장 완료 응답을 확인하지 못했습니다. 서버에 저장되었을 수 있습니다.');
        } finally {
          clearTimeout(timeout); draftSavePending = false;
        }
      })();
    });
  }
"""
