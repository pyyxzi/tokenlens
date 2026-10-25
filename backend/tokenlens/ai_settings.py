"""Durable per-feature model selections. Credentials remain in local profiles."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import threading
import time
import uuid

from . import ccswitch, connection_models, promptlab
from .reasoning import LEVELS as THINKING_LEVELS
from .credential_safety import contains_secret, same_origin, validate_destination, sanitize_public

TARGETS = {'assistant','prompt'}
PROTOCOLS = {'openai','responses','anthropic','gemini'}
CHECK_TIMEOUT = 10.0
DEFAULT_THINKING = 'auto'


class AISettings:
    def __init__(self, app):
        self.app=app
        self.path=Path(app.prompt.path).parent/'ai-settings.json'
        self.lock=threading.RLock()
        self._checks={}

    def _read(self):
        try:
            with self.path.open('rb') as stream:
                raw=stream.read(65537)
            if len(raw)>65536:return {}
            obj=json.loads(raw.decode('utf-8'))
            assignments=obj.get('assignments',{}) if isinstance(obj,dict) else {}
            if not isinstance(assignments,dict):return {}
            clean={}
            for target,selection in assignments.items():
                if target not in TARGETS or not isinstance(selection,dict):continue
                limits={'connection_id':200,'model':256,'protocol':20,'thinking':20}
                if any(not isinstance(selection.get(key,''),str) or len(selection.get(key,''))>limit
                       for key,limit in limits.items()):continue
                clean[target]={key:selection[key] for key in limits if key in selection}
            return clean
        except (OSError,ValueError,UnicodeError,RecursionError):
            return {}

    def _write(self, assignments):
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version':1,'assignments':assignments},ensure_ascii=False),encoding='utf-8')
        os.replace(temporary,self.path)

    def connections(self):
        profiles=[{'id':'profile:'+row['id'],'name':row['name'],'model':row['model'],
                   'protocol':row['protocol_effective'],'source':'profile','configured':row['configured'],
                   'base_url':row['base_url'],'editable':True,
                   'has_key':bool(self.app.prompt.api_key(row['id']))} for row in self.app.prompt.profiles_public()]
        for row in profiles:
            key=self.app.prompt.api_key(row['id'][8:])
            if key:
                for field in ('name','model','base_url'):
                    row[field]=sanitize_public(str(row[field]), key)
        local=[{'id':row['id'],'name':row['name'],'model':row.get('model',''),'protocol':row['protocol'],
                'source':'ccswitch','configured':row['configured'],'reason':row.get('reason',''),
                'app_type':row.get('app_type',''),'editable':False} for row in ccswitch.catalog().get('connections',[])]
        return profiles+local

    def selection(self, target):
        if target not in TARGETS:raise ValueError('不支持的 AI 功能。')
        with self.lock:
            selected=self._read().get(target)
        if isinstance(selected,dict):
            # Older assignments predate the reasoning control; keep the stored
            # shape stable and surface the default so callers need no fallback.
            return {**selected,'thinking':self._level(selected.get('thinking'))}
        rows=self.connections()
        active='profile:'+self.app.prompt.active_profile_id
        row=next((row for row in rows if row['id']==active and row['configured']),None)
        if not row:row=next((row for row in rows if row['configured'] and row['model']),None)
        return {'connection_id':row['id'],'model':row['model'],'protocol':row['protocol'],
                'thinking':DEFAULT_THINKING} if row else {}

    @staticmethod
    def _level(value):
        """Coerce a stored reasoning level; unknown values fall back to auto."""
        level=str(value or '').strip().lower()
        return level if level in THINKING_LEVELS else DEFAULT_THINKING

    def thinking(self, target):
        """Reasoning level for one feature. Never part of the resolve payload."""
        return self._level(self.selection(target).get('thinking'))

    def _resolve(self, selection, missing_model=False):
        identifier=selection.get('connection_id','');model=selection.get('model','');protocol=selection.get('protocol','')
        if protocol not in PROTOCOLS:raise ValueError('请选择有效的请求协议。')
        if identifier.startswith('ccs_'):
            return ccswitch.resolve(identifier,model,protocol=protocol,allow_missing_model=missing_model)
        if not identifier.startswith('profile:'):raise ValueError('请选择已保存连接或自定义连接。')
        profile_id=identifier[8:]
        profile=self.app.prompt.profiles.get(profile_id)
        if not profile:raise ValueError('所选 AI 连接已删除，请到设置中重新选择。')
        key=self.app.prompt.api_key(profile_id)
        model=model.strip() or profile.get('model','')
        if not key or not profile.get('base_url') or not missing_model and not model:raise ValueError('连接缺少模型、地址或 API Key。')
        validate_destination(profile['base_url'], key)
        if contains_secret(model, key):raise ValueError('模型 ID 不能包含 API Key。')
        return {'model':model,'base_url':profile['base_url'],'api_key':key,'protocol':protocol}

    def resolve(self, target):
        return self._resolve(self.selection(target))

    def public(self):
        rows=self.connections();out={}
        for target in ('assistant','prompt'):
            selection=self.selection(target);row=next((row for row in rows if row['id']==selection.get('connection_id')),None)
            error=''
            try:self._resolve(selection)
            except (ValueError,ccswitch.CCSwitchError) as exc:error=str(exc)
            identifier=selection.get('connection_id','')
            key=self.app.prompt.api_key(identifier[8:]) if identifier.startswith('profile:') and identifier[8:] in self.app.prompt.profiles else ''
            visible=sanitize_public(selection, key)
            out[target]={**visible,'name':row['name'] if row else '', 'configured':not bool(error), 'error':error}
        return {'ok':True,'connections':rows,'assignments':out}

    @staticmethod
    def _text(body,key,maximum=256):
        value=body.get(key,'')
        if not isinstance(value,str) or len(value)>maximum:raise ValueError('无效字段：'+key)
        return value.strip()

    def _connection_input(self, body, fields, existing_key='', existing_url=''):
        if not isinstance(fields,dict):raise ValueError('请填写自定义连接。')
        base=self._text(fields,'base_url',2048);key=self._text(fields,'api_key',4096) or existing_key
        model=self._text(body,'model');protocol=self._text(body,'protocol')
        if protocol not in PROTOCOLS:raise ValueError('请求协议无效。')
        validate_destination(base, key)
        if existing_key and not self._text(fields,'api_key',4096) and existing_url and not same_origin(base, existing_url):
            raise ValueError('接口地址已改变，请重新填写 API Key，不能将保存的密钥发往其他服务。')
        if not base or not key:raise ValueError('请填写 API URL 和 API Key。')
        if any(contains_secret(value, key) for value in (base, model, self._text(fields,'name',200))):
            raise ValueError('请将 API Key 仅填写在密钥字段中。')
        return {'model':model,'base_url':base,'api_key':key,'protocol':protocol}

    def _persist_profile(self, fields, connection, identifier=''):
        model=connection['model'];base=connection['base_url'];key=connection['api_key'];protocol=connection['protocol']
        if not model:raise ValueError('请填写模型 ID。')
        name=self._text(fields,'name',200) or model
        lab=self.app.prompt
        with lab._lock:
            if identifier and identifier not in lab.profiles:raise ValueError('所选 AI 连接已删除，请重新选择。')
            if not identifier and len(lab.profiles)>=lab.MAX_PROFILES:raise ValueError('AI 配置数量已达上限，请先删除不再使用的连接。')
            if lab.active_profile_id:lab.profiles[lab.active_profile_id]=copy.deepcopy(lab.data)
            profile=copy.deepcopy(lab.profiles[identifier]) if identifier else lab._blank_profile()
            replace_key=not identifier or bool(self._text(fields,'api_key',4096))
            if not identifier:identifier='ai_'+uuid.uuid4().hex[:12]
            profile.update(name=name,model=model,base_url=promptlab.normalize_base_url(base),protocol=protocol,
                           saved_at=int(time.time()),verified_at=int(time.time()))
            if replace_key:
                encrypted,scheme=promptlab.protect_secret(key)
                profile.update(key_enc=encrypted if scheme=='dpapi' else None,
                               key_scheme='dpapi' if scheme=='dpapi' else 'session',
                               key_plain=None,key_hint=promptlab.mask_key(key))
                if scheme!='dpapi':lab._session_keys[identifier]=key
                else:lab._session_keys.pop(identifier,None)
            lab.profiles[identifier]=profile
            if identifier==lab.active_profile_id:lab.data=copy.deepcopy(profile)
            lab._flush()
        return 'profile:'+identifier

    def _custom(self, body, save=False):
        fields=body.get('custom');connection=self._connection_input(body,fields)
        return self._persist_profile(fields,connection) if save else connection

    def _profile_edit(self, body, selection, save=False):
        identifier=selection['connection_id']
        if not identifier.startswith('profile:'):raise ValueError('只能编辑 TokenLens 中保存的连接。')
        profile_id=identifier[8:]
        if profile_id not in self.app.prompt.profiles:raise ValueError('所选 AI 连接已删除，请重新选择。')
        fields=body.get('edit')
        connection=self._connection_input(body,fields,self.app.prompt.api_key(profile_id),
                                          self.app.prompt.profiles[profile_id].get('base_url',''))
        return self._persist_profile(fields,connection,profile_id) if save else connection

    def _draft(self, body):
        target=self._text(body,'target',20)
        if target not in TARGETS:raise ValueError('不支持的 AI 功能。')
        selection={key:self._text(body,key,200 if key=='connection_id' else 256) for key in ('connection_id','model','protocol')}
        if not selection['model']:raise ValueError('请填写模型 ID。')
        requested=self._text(body,'thinking',20) or DEFAULT_THINKING
        if requested.lower() not in THINKING_LEVELS:raise ValueError('思考强度无效。')
        selection['thinking']=requested.lower()
        editing='edit' in body
        if editing:connection=self._profile_edit(body,selection)
        else:connection=self._custom(body) if selection['connection_id']=='custom' else self._resolve(selection)
        if contains_secret(connection['model'], connection['api_key']):raise ValueError('模型 ID 不能包含 API Key。')
        fingerprint=promptlab.PromptLab.fingerprint(connection['model'],connection['base_url'],connection['api_key'],connection['protocol'],False,False)
        fields=body.get('edit') if editing else body.get('custom') if selection['connection_id']=='custom' else None
        name=self._text(fields,'name',200) if fields is not None else ''
        return target,selection,connection,(target,selection['connection_id'],fingerprint,editing,name)

    def check(self, body, cancel=None):
        _,_,connection,fingerprint=self._draft(body)
        # Reuse the existing tiny streaming probe, without a preceding catalog
        # request. This proves the selected inference route/model, not just /models.
        try:
            result=self.app.prompt.check(**connection,quick=False,timeout=CHECK_TIMEOUT,
                                         hedge=False,first_output_only=True,
                                         **({'cancel':cancel} if cancel is not None else {}))
        except Exception:
            result={'ok':False,'code':'unreachable','detail':'自检请求失败，请检查连接与服务商状态后重试。'}
        with self.lock:
            now=time.time()
            self._checks={key:value for key,value in self._checks.items() if now-value<self.app.prompt.VERIFY_TTL}
            if result.get('ok'):
                if len(self._checks)>=128:self._checks.pop(next(iter(self._checks)))
                self._checks[fingerprint]=now
            else:self._checks.pop(fingerprint,None)
        # Keep provider error echoes and response content out of the picker.
        public=sanitize_public({key:result.get(key) for key in ('ok','code','title','detail','hint','timing','checked_at')}, connection['api_key'])
        public.update(expires_in=self.app.prompt.VERIFY_TTL if result.get('ok') else 0,probe_mode='stream',cached=False)
        return public

    def save(self, body):
        with self.lock:
            target,selection,_,fingerprint=self._draft(body)
            checked=self._checks.get(fingerprint)
            if checked is None or time.time()-checked>=self.app.prompt.VERIFY_TTL:
                raise ValueError('请先通过快速自检再保存；修改连接后需要重新自检（15 分钟内有效）。')
            if 'edit' in body:selection['connection_id']=self._profile_edit(body,selection,save=True)
            elif selection['connection_id']=='custom':selection['connection_id']=self._custom(body,save=True)
            self._resolve(selection)
            assignments=self._read();assignments[target]=selection;self._write(assignments)
        return {**self.public(),'selection':self.public()['assignments'][target]}

    def models(self, body):
        selection={key:self._text(body,key,200 if key=='connection_id' else 256) for key in ('connection_id','model','protocol')}
        if 'edit' in body:connection=self._profile_edit(body,selection)
        elif selection['connection_id']=='custom':connection=self._custom(body)
        elif selection['connection_id'].startswith('ccs_'):return connection_models.discover(selection['connection_id'],selection['protocol'])
        else:connection=self._resolve(selection,missing_model=True)
        result=promptlab.models_probe(connection['base_url'],connection['api_key'],proto=connection['protocol'],timeout=15)
        models=[{'id':value,'group':''} for value in result.get('models',[]) if isinstance(value,str) and len(value)<=256 and connection['api_key'] not in value]
        return {'ok':bool(result.get('ok')),'models':models,'message':'' if result.get('ok') else '服务商未返回模型目录，可以直接填写模型 ID。'}
