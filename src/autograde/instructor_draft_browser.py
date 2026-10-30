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
    document.addEventListener('submit', event => {
      if (draftSavePending) {
        event.preventDefault(); announce('저장 결과를 확인하고 있습니다. 완료 후 다음 작업을 진행하세요.'); return;
      }
      const form = event.target;
      if (!inlineForms.includes(form) || event.defaultPrevented) return;
      event.preventDefault();
      if (needsReview || form.hasAttribute('data-save-conflict')) {
        showStatus(form, '서버의 최신 초안을 새 탭에서 확인하세요. 현재 입력은 이 탭에 유지되며 자동으로 다시 전송하지 않습니다.'); return;
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
      const fileFields = [...form.querySelectorAll('input[type=file]')];
      const chosen = new Map(fileFields.map(el => [el, el.files]));
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
            if (response.status === 409 || response.status === 401 || response.status === 403 || response.status === 404) needsReview = true;
            // Only a known input rejection is safe to correct and retry. Other
            // failures can occur after a commit, so keep the original fence.
            if (response.status !== 400 || receipt?.error?.code !== 'draft_input') needsReview = true;
            showStatus(form, needsReview ? '저장 결과 또는 권한을 확인해야 합니다. 입력·파일 선택은 유지했습니다. 서버의 최신 초안을 새 탭에서 확인하세요.' :
              `저장되지 않았습니다 · ${receipt.error.message} 입력·파일 선택은 유지했습니다.`);
            announce(needsReview ? '서버 최신 상태 확인 필요 · 자동 재전송하지 않습니다.' : '입력을 수정한 뒤 이 영역만 다시 저장하세요.');
            return;
          }
          if (receipt?.ok !== true || receipt.path !== path || receipt.previous_revision !== previous || receipt.revision !== previous + 1 ||
              !Array.isArray(receipt.summary) || receipt.summary.length !== 5 || !receipt.values || !receipt.uploads || typeof receipt.can_check !== 'boolean') {
            throw new Error('unconfirmed');
          }
          const unchanged = fingerprint(form) === before;
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
          }
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
        } catch (_) {
          needsReview = true;
          showStatus(form, '저장 완료 응답을 확인하지 못했습니다. 입력·파일 선택은 유지했습니다. 서버에 저장되었을 수 있으므로 최신 초안을 새 탭에서 확인하세요. 자동으로 다시 전송하지 않습니다.');
          announce('연결 또는 응답 확인 필요 · 현재 입력을 유지하고 자동 재전송을 중단했습니다.');
        } finally {
          clearTimeout(timeout); draftSavePending = false;
        }
      })();
    });
  }
"""
