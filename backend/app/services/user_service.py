"""
用户认证、资料与角色关联服务。

``UserService`` 负责注册唯一性检查、密码哈希、登录认证、JWT 签发以及用户资料事务；
``RoleService`` 负责角色表和用户-角色关联。服务层返回领域对象或抛出可识别异常，API
依赖层再决定当前请求是否具有普通用户、管理员或超级用户权限。
"""
import logging
from typing import List, Optional
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, desc

from app.models.user import User, UserRole, Role, UserRoleAssociation
from app.schemas.user import UserCreate, UserUpdate
from app.schemas.auth import Token
from app.core.security import get_password_hash, verify_password, create_access_token

logger = logging.getLogger(__name__)


class UserService:
    """封装用户认证、资料权限和用户事务，并把角色关联交给独立服务。"""

    def __init__(self, db: AsyncSession):
        """
        初始化UserService实例

        Args:
            db: 数据库会话实例
        """
        self.db = db

    async def register_user(self, user_data: UserCreate) -> User:
        """
        注册新用户
        
        Args:
            user_data: 用户创建数据
            
        Returns:
            创建的用户对象
            
        Raises:
            ValueError: 当邮箱或用户名已存在时
        """
        # 先分别检查邮箱和用户名，便于向调用方返回明确冲突原因。
        # create_user 会再次检查，缩小注册入口之外直接调用该方法时的风险；最终并发一致性仍依赖数据库唯一约束。
        existing_user = await self.get_user_by_email(user_data.email)
        if existing_user:
            raise ValueError("邮箱已被注册")

        existing_user = await self.get_user_by_username(user_data.username)
        if existing_user:
            raise ValueError("用户名已被占用")

        # 唯一性通过后进入真正的哈希、建模与提交阶段。
        return await self.create_user(user_data)

    async def login_user(self, username_or_email: str, password: str) -> dict:
        """
        用户登录并生成访问令牌
        
        Args:
            username_or_email: 用户名或邮箱
            password: 密码
            
        Returns:
            包含访问令牌的字典
            
        Raises:
            ValueError: 当认证失败或用户未激活时
        """
        import logging
        from app.core.config import settings
        from datetime import timedelta
        
        logger = logging.getLogger(__name__)
        
        logger.info(f"登录尝试，用户名: {username_or_email}")
        
        # authenticate 同时支持用户名和邮箱，并在密码正确后更新最后登录时间。
        user = await self.authenticate(username_or_email, password)

        # 未找到用户、密码错误或认证查询异常都统一为同一提示，避免泄露账号是否存在。
        if not user:
            logger.warning(f"认证失败，用户名: {username_or_email}")
            raise ValueError("用户名或密码错误")

        # 当前用户名/邮箱查询已过滤停用用户，因此停用账号通常会在上面表现为认证失败；
        # 此处仍保留二次状态防线，防止未来认证查询范围调整后直接签发令牌。
        if not user.is_active:
            logger.warning(f"非活跃用户尝试登录: {username_or_email}")
            raise ValueError("用户未激活")

        # JWT 的 sub 只保存稳定用户 id；后续请求由认证依赖据此重新读取用户和权限状态。
        logger.info(f"为用户创建访问令牌: {user.username}")
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": str(user.id)}, expires_delta=access_token_expires
        )

        logger.info(f"用户登录成功: {user.username}")
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        }

    async def refresh_user_token(self, user_id: str) -> dict:
        """
        刷新用户访问令牌
        
        Args:
            user_id: 用户ID
            
        Returns:
            包含新访问令牌的字典
        """
        from app.core.config import settings
        from datetime import timedelta

        # 本方法只根据传入 user_id 重签令牌，不在这里重新查询用户是否存在、是否停用或权限是否变化；
        # 这些校验必须由调用它的端点/依赖在进入服务前完成。
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": str(user_id)}, expires_delta=access_token_expires
        )

        return {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        }

    async def get_user_with_roles(self, user_id: str) -> dict:
        """
        获取用户信息及其角色
        
        Args:
            user_id: 用户ID
            
        Returns:
            包含用户信息和角色列表的字典
        """
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            logger.info(f"📋 获取用户信息，用户ID: {user_id}")
            
            # 获取用户信息
            user = await self.get_user_by_id(user_id)
            if not user:
                raise ValueError("用户不存在")
            
            # 获取角色信息
            role_service = RoleService(self.db)
            roles = await role_service.list_user_roles(user.id)
            
            return {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "phone": user.phone,
                "department": user.department,
                "position": user.position,
                "employee_id": user.employee_id,
                "role": user.role,
                "is_superuser": user.is_superuser,
                "is_verified": user.is_verified,
                "is_active": user.is_active,
                "bio": user.bio,
                "avatar_url": user.avatar_url,
                "last_login": user.last_login,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
                "roles": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "description": r.description,
                        "is_builtin": r.is_builtin,
                        "created_at": r.created_at,
                        "updated_at": r.updated_at,
                    }
                    for r in roles
                ],
            }
        except Exception as e:
            logger.error(f"❌ 获取用户信息时出错: {e}")
            raise

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """
        通过ID获取用户（兼容字符串ID）
        
        Args:
            user_id: 用户ID（字符串或UUID）
            
        Returns:
            用户对象或None
        """
        try:
            from uuid import UUID
            if isinstance(user_id, str):
                user_id = UUID(user_id)
            return await self.get_user(user_id)
        except Exception as e:
            logger.error(f"通过ID获取用户时出错: {e}")
            return None

    async def create_user(self, user_data: UserCreate) -> User:
        """校验邮箱/用户名唯一性、哈希密码并创建用户，再尝试分配默认角色。

        用户记录先独立提交，默认角色关联随后第二次提交且失败会被吸收，因此两个写入不是原子
        事务：方法可能成功返回一个没有“普通用户”角色的新用户。
        """
        try:
            # 检查用户是否已存在
            existing_user = await self.get_user_by_email(user_data.email)
            if existing_user:
                raise ValueError("使用此邮箱的用户已存在")

            # 检查用户名是否已被占用
            existing_username = await self.get_user_by_username(user_data.username)
            if existing_username:
                raise ValueError("用户名已被占用")

            # 明文密码只在请求对象中短暂存在，写入 ORM 前转换为不可逆哈希。
            hashed_password = get_password_hash(user_data.password)

            # 公开资料映射到 User；不把 password 原字段写入数据库。
            user = User(
                username=user_data.username,
                email=user_data.email,
                hashed_password=hashed_password,
                full_name=user_data.full_name,
                phone=user_data.phone,
                department=user_data.department,
                position=user_data.position,
                employee_id=user_data.employee_id,
                bio=user_data.bio
            )

            self.db.add(user)
            # 先提交用户以取得数据库生成的 id；refresh 将 id、时间戳等回填到 ORM 对象。
            await self.db.commit()
            await self.db.refresh(user)

            # 默认角色在另一个提交中创建。角色分配失败会被 _assign_default_role 吸收，
            # 已提交的用户不会回滚，因此可能出现“用户存在但没有默认角色”的部分成功状态。
            await self._assign_default_role(user)

            logger.info(f"创建了用户{user.id}，邮箱为{user.email}")
            return user

        except Exception as e:
            # 只能撤销当前未提交事务；若用户提交后默认角色阶段失败，用户记录已经持久化。
            await self.db.rollback()
            logger.error(f"创建用户时出错: {e}")
            raise

    async def authenticate(self, username_or_email: str, password: str) -> Optional[User]:
        """
        通过用户名或邮箱和密码验证用户

        Args:
            username_or_email: 用户名或邮箱
            password: 密码

        Returns:
            验证成功的用户对象，验证失败返回None
        """
        try:
            # 为避免要求调用方预先判断输入类型，先按用户名查询，未命中再按邮箱查询。
            user = await self.get_user_by_username(username_or_email)
            if not user:
                user = await self.get_user_by_email(username_or_email)

            if not user:
                return None

            # 只比较密码哈希；失败与用户不存在都返回 None，交给 login_user 统一错误信息。
            if not verify_password(password, user.hashed_password):
                return None

            # 密码验证成功后单独提交最后登录时间，再把用户交给登录流程执行防御性状态检查并签发 JWT。
            await self._update_last_login(user.id)

            return user

        except Exception as e:
            logger.error(f"验证用户时出错: {e}")
            # 数据库或哈希校验异常同样被收敛为认证失败，不向登录端点暴露内部原因。
            return None

    async def get_user(self, user_id: UUID, include_inactive: bool = False) -> Optional[User]:
        """
        通过ID获取用户

        Args:
            user_id: 用户ID
            include_inactive: 是否包含停用用户，默认为False
        """
        try:
            if include_inactive:
                # 获取任何用户（包括停用的用户）
                return await self.db.get(User, user_id)
            else:
                # 只获取活跃的用户
                query = select(User).where(User.id == user_id, User.is_active == True)
                result = await self.db.execute(query)
                return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"获取用户{user_id}时出错: {e}")
            raise

    async def get_user_by_email(self, email: str) -> Optional[User]:
        """按邮箱读取启用用户；停用账号与未命中都返回 ``None``。"""
        try:
            query = select(User).where(User.email == email, User.is_active == True)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"通过邮箱{email}获取用户时出错: {e}")
            raise

    async def get_user_by_username(self, username: str) -> Optional[User]:
        """按用户名读取启用用户；停用账号与未命中都返回 ``None``。"""
        try:
            query = select(User).where(User.username == username, User.is_active == True)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            logger.error(f"通过用户名{username}获取用户时出错: {e}")
            raise

    async def get_users(
        self,
        skip: int = 0,
        limit: int = 20,
        role: Optional[UserRole] = None,
        department: Optional[str] = None,
        include_inactive: bool = False
    ) -> List[User]:
        """
        获取用户列表

        Args:
            skip: 跳过的记录数
            limit: 返回的记录数
            role: 按角色过滤
            department: 按部门过滤
            include_inactive: 是否包含停用用户，默认为False
        """
        try:
            # 构建基础查询
            query = select(User)

            # 根据include_inactive参数决定是否只查询活跃用户
            if not include_inactive:
                query = query.where(User.is_active == True)

            # 应用过滤条件
            if role:
                query = query.where(User.role == role)

            if department:
                query = query.where(User.department == department)

            # 应用排序和分页
            query = query.order_by(desc(User.created_at)).offset(skip).limit(limit)

            result = await self.db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"获取用户时出错: {e}")
            raise

    async def get_users_with_roles(
        self,
        skip: int = 0,
        limit: int = 100
    ) -> List[dict]:
        """
        获取所有用户及其角色信息

        Args:
            skip: 跳过的记录数
            limit: 每页记录数

        Returns:
            包含用户信息和角色信息的字典列表
        """
        try:
            # 获取所有用户（包括停用用户）
            users = await self.get_users(skip=skip, limit=limit, include_inactive=True)

            # 获取角色服务
            role_service = RoleService(self.db)
            user_ids = [u.id for u in users]
            roles_map = await role_service.get_roles_for_users(user_ids)

            result: List[dict] = []
            for u in users:
                roles = roles_map.get(u.id, [])
                role_names = {r.name for r in roles}
                # 仅根据角色表确定管理员身份
                derived_role = UserRole.ADMIN if "超级管理员" in role_names else UserRole.EMPLOYEE
                result.append({
                    "id": u.id,
                    "username": u.username,
                    "email": u.email,
                    "full_name": u.full_name,
                    "phone": u.phone,
                    "department": u.department,
                    "position": u.position,
                    "employee_id": u.employee_id,
                    "role": derived_role,
                    "is_superuser": u.is_superuser,
                    "is_verified": u.is_verified,
                    "is_active": u.is_active,
                    "bio": u.bio,
                    "avatar_url": u.avatar_url,
                    "last_login": u.last_login,
                    "created_at": u.created_at,
                    "updated_at": u.updated_at,
                    "roles": [
                        {
                            "id": r.id,
                            "name": r.name,
                            "description": r.description,
                            "is_builtin": r.is_builtin,
                            "created_at": r.created_at,
                            "updated_at": r.updated_at,
                        }
                        for r in roles
                    ],
                })
            return result
        except Exception as e:
            logger.error(f"获取用户列表时出错: {e}")
            raise
    async def update_user(
        self,
        user_id: UUID,
        user_data: UserUpdate,
        current_user: User
    ) -> Optional[User]:
        """
        更新用户信息

        Args:
            user_id: 用户ID
            user_data: 用户更新数据
            current_user: 当前操作用户

        Returns:
            更新后的用户对象，如果用户不存在则返回None
        """
        try:
            # 先读取目标实体；权限检查必须发生在接受任何更新字段之前。
            user = await self.db.get(User, user_id)
            if not user:
                return None

            # 普通用户只能更新自己，超级用户可更新任意用户；具体规则集中在 can_update_user。
            if not self.can_update_user(current_user, user.id):
                raise PermissionError("权限不足，无法更新此用户")

            # 局部更新只保留客户端实际提交的字段；password 必须排除，不能直接落入 User 列。
            update_data = user_data.dict(exclude_unset=True, exclude={'password'})

            # 密码走独立哈希分支，最终写入 hashed_password。
            if user_data.password:
                update_data['hashed_password'] = get_password_hash(user_data.password)

            # 只有邮箱/用户名确实发生变化时才查询冲突，避免无意义数据库访问。
            if 'email' in update_data and update_data['email'] != user.email:
                existing_email = await self.get_user_by_email(update_data['email'])
                if existing_email and existing_email.id != user_id:
                    raise ValueError("邮箱已被使用")

            if 'username' in update_data and update_data['username'] != user.username:
                existing_username = await self.get_user_by_username(update_data['username'])
                if existing_username and existing_username.id != user_id:
                    raise ValueError("用户名已被占用")

            # 有实际变更时才执行 UPDATE 和提交；refresh 使先前读取的 ORM 对象获得数据库新值。
            if update_data:
                query = (
                    update(User)
                    .where(User.id == user_id)
                    .values(**update_data)
                )
                await self.db.execute(query)
                await self.db.commit()
                await self.db.refresh(user)

            logger.info(f"更新了用户{user_id}")
            return user

        except Exception as e:
            await self.db.rollback()
            logger.error(f"更新用户{user_id}时出错: {e}")
            raise

    async def delete_user(
        self,
        user_id: UUID,
        current_user: User
    ) -> bool:
        """
        软删除用户

        Args:
            user_id: 用户ID
            current_user: 当前操作用户

        Returns:
            删除成功返回True，用户不存在返回False
        """
        try:
            # 获取要删除的用户
            user = await self.get_user(user_id)
            if not user:
                return False

            # 检查权限
            if not self.can_delete_user(current_user, user.id):
                raise PermissionError("权限不足，无法删除此用户")

            # 通过将is_active设置为False进行软删除
            query = (
                update(User)
                .where(User.id == user_id)
                .values(is_active=False)
            )

            await self.db.execute(query)
            await self.db.commit()

            logger.info(f"删除了用户{user_id}")
            return True

        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除用户{user_id}时出错: {e}")
            raise

    async def search_users(
        self,
        query: str,
        current_user: User,
        limit: int = 10
    ) -> List[User]:
        """
        按姓名、邮箱或用户名搜索用户

        Args:
            query: 搜索关键字
            current_user: 当前操作用户
            limit: 返回结果数量限制

        Returns:
            匹配的用户列表
        """
        try:
            # 仅允许HR和管理员用户搜索
            if current_user.role not in [UserRole.HR_MANAGER, UserRole.HR_SPECIALIST, UserRole.ADMIN]:
                raise PermissionError("权限不足，无法搜索用户")

            search_query = (
                select(User)
                .where(
                    (
                        User.full_name.ilike(f"%{query}%") |
                        User.email.ilike(f"%{query}%") |
                        User.username.ilike(f"%{query}%")
                    )
                )
                .limit(limit)
            )

            result = await self.db.execute(search_query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"搜索用户时出错: {e}")
            raise

    async def _update_last_login(self, user_id: UUID) -> None:
        """用数据库时间更新最后登录字段并立即提交；失败只记录日志，不阻断认证。

        当前异常分支不执行 ``rollback``，因此提交阶段失败后复用同一会话可能需要调用方先
        恢复事务状态。
        """
        try:
            from sqlalchemy import func
            query = (
                update(User)
                .where(User.id == user_id)
                .values(last_login=func.now())
            )
            await self.db.execute(query)
            await self.db.commit()

        except Exception as e:
            logger.error(f"更新用户{user_id}的最后登录时间时出错: {e}")

    def can_view_user(self, current_user: User, target_user_id: UUID) -> bool:
        """
        检查当前用户是否可以查看目标用户信息

        Args:
            current_user: 当前用户
            target_user_id: 目标用户ID

        Returns:
            是否有权限查看
        """
        # 用户可以查看自己的个人资料
        if current_user.id == target_user_id:
            return True

        # 管理员可以查看任何人
        if current_user.is_superuser:
            return True

        return False

    def can_update_user(self, current_user: User, target_user_id: UUID) -> bool:
        """
        检查当前用户是否可以更新目标用户信息

        Args:
            current_user: 当前用户
            target_user_id: 目标用户ID

        Returns:
            是否有权限更新
        """
        # 用户可以更新自己的个人资料
        if current_user.id == target_user_id:
            return True

        # 管理员可以更新任何人
        if current_user.is_superuser:
            return True

        return False

    def can_delete_user(self, current_user: User, target_user_id: UUID) -> bool:
        """
        检查当前用户是否可以删除目标用户

        Args:
            current_user: 当前用户
            target_user_id: 目标用户ID

        Returns:
            是否有权限删除
        """
        # 只有管理员可以删除用户
        if current_user.is_superuser:
            return True

        return False

    async def _assign_default_role(self, user: User) -> None:
        """为新用户分配默认的"普通用户"角色"""
        try:
            # 默认角色按名称查找；角色表未初始化时不创建关联，用户仍保持已注册状态。
            from sqlalchemy import select
            result = await self.db.execute(select(Role).where(Role.name == "普通用户"))
            default_role = result.scalar_one_or_none()

            if default_role:
                user_role_assoc = UserRoleAssociation(user_id=user.id, role_id=default_role.id)
                self.db.add(user_role_assoc)
                # 角色关联使用独立事务，不能与 create_user 中已经提交的用户记录原子回滚。
                await self.db.commit()
        except Exception as e:
            logger.error(f"为用户{user.id}分配默认角色时出错: {e}")
            # 异常只回滚关联写入且不继续抛出，注册主流程仍会返回成功用户。
            await self.db.rollback()


class RoleService:
    """管理角色记录及用户-角色关联；角色替换当前采用两阶段提交。"""

    def __init__(self, db: AsyncSession):
        """
        初始化RoleService实例

        Args:
            db: 数据库会话实例
        """
        self.db = db

    def is_admin_user(self, user: User) -> bool:
        """
        检查用户是否为管理员

        Args:
            user: 用户对象

        Returns:
            是否为管理员
        """
        return user.is_superuser

    async def list_roles(self) -> List[Role]:
        """获取所有角色列表"""
        try:
            result = await self.db.execute(select(Role).where(Role.is_active == True).order_by(desc(Role.created_at)))
            return result.scalars().all()
        except Exception as e:
            logger.error(f"获取角色列表时出错: {e}")
            raise

    async def create_role(self, name: str, description: Optional[str] = None, is_builtin: bool = False) -> Role:
        """
        创建新角色

        Args:
            name: 角色名称
            description: 角色描述
            is_builtin: 是否为内置角色

        Returns:
            创建的角色对象
        """
        try:
            existing = await self.db.execute(select(Role).where(Role.name == name))
            if existing.scalar_one_or_none():
                raise ValueError("角色名称已存在")
            role = Role(name=name, description=description, is_builtin=is_builtin)
            self.db.add(role)
            await self.db.commit()
            await self.db.refresh(role)
            return role
        except Exception as e:
            await self.db.rollback()
            logger.error(f"创建角色时出错: {e}")
            raise

    async def delete_role(self, role_id: UUID) -> bool:
        """
        删除角色

        Args:
            role_id: 角色ID

        Returns:
            删除成功返回True，角色不存在返回False
        """
        try:
            role = await self.db.get(Role, role_id)
            if not role:
                return False
            await self.db.delete(role)
            await self.db.commit()
            return True
        except Exception as e:
            await self.db.rollback()
            logger.error(f"删除角色{role_id}时出错: {e}")
            raise

    async def list_user_roles(self, user_id: UUID) -> List[Role]:
        """
        获取用户的角色列表

        Args:
            user_id: 用户ID

        Returns:
            用户的角色列表
        """
        try:
            result = await self.db.execute(
                select(Role)
                .join(UserRoleAssociation, Role.id == UserRoleAssociation.role_id)
                .where(UserRoleAssociation.user_id == user_id, Role.is_active == True)
                .order_by(desc(Role.created_at))
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"获取用户{user_id}的角色列表时出错: {e}")
            raise

    async def assign_roles_to_user(self, user_id: UUID, role_ids: List[UUID]) -> List[Role]:
        """
        为用户分配角色

        Args:
            user_id: 用户ID
            role_ids: 角色ID列表

        Returns:
            分配给用户的角色列表
        """
        try:
            # 在改动关联表前先确认用户和每个角色都存在，避免生成悬空外键。
            if not await self.db.get(User, user_id):
                raise ValueError("用户不存在")

            for rid in role_ids:
                if not await self.db.get(Role, rid):
                    raise ValueError(f"角色不存在: {rid}")

            # 当前实现先删除旧关联并立即提交，再创建新关联并第二次提交。
            # 这不是单个原子事务：若第二阶段失败，用户会暂时或永久处于无角色状态。
            await self.db.execute(
                delete(UserRoleAssociation).where(UserRoleAssociation.user_id == user_id)
            )
            await self.db.commit()

            # 输入列表已在上方逐项验证，此处只负责建立新的多对多关联。
            for rid in role_ids:
                self.db.add(UserRoleAssociation(user_id=user_id, role_id=rid))

            await self.db.commit()

            # 重新显式查询数据库中的最终角色，避免在异步会话中触发关联属性懒加载。
            return await self.list_user_roles(user_id)
        except Exception as e:
            await self.db.rollback()
            logger.error(f"为用户{user_id}分配角色时出错: {e}")
            raise

    async def get_roles_for_users(self, user_ids: List[UUID]) -> dict[UUID, List[Role]]:
        """
        获取多个用户的角色信息

        Args:
            user_ids: 用户ID列表

        Returns:
            以用户ID为键，角色列表为值的字典
        """
        try:
                if not user_ids:
                    return {}
                result = await self.db.execute(
                    select(UserRoleAssociation.user_id, Role)
                    .join(Role, Role.id == UserRoleAssociation.role_id)
                    .where(UserRoleAssociation.user_id.in_(user_ids), Role.is_active == True)
                )
                rows = result.all()
                mapping: dict[UUID, List[Role]] = {}
                for uid, role in rows:
                    mapping.setdefault(uid, []).append(role)
                return mapping
        except Exception as e:
            logger.error(f"获取用户角色信息时出错: {e}")
            raise
